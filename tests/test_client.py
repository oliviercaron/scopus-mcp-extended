import unittest
from unittest.mock import MagicMock, patch, AsyncMock

import httpx

from scopus_mcp.client import (
    ScopusClient,
    ScopusAccessError,
    ScopusAuthError,
    ScopusSubscriptionError,
    _build_advice,
)

class TestScopusClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Patch get_api_key to avoid needing config file in tests
        self.config_patcher = patch('scopus_mcp.client.get_api_key', return_value='fake_key')
        self.mock_get_key = self.config_patcher.start()
        
        # Patch CacheManager to avoid disk I/O
        self.cache_patcher = patch('scopus_mcp.client.CacheManager')
        self.MockCache = self.cache_patcher.start()
        self.mock_cache_instance = self.MockCache.return_value
        self.mock_cache_instance.get.return_value = None # Default no cache hit

        self.client = ScopusClient()

    async def asyncTearDown(self):
        self.config_patcher.stop()
        self.cache_patcher.stop()
        await self.client.close()

    @patch('scopus_mcp.client.httpx.AsyncClient.request')
    async def test_search_scopus_success(self, mock_request):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'search-results': {'entry': []}}
        
        # httpx.AsyncClient.request is awaitable
        mock_request.return_value = mock_response

        result = await self.client.search_scopus("AI")
        
        self.assertEqual(result, {'search-results': {'entry': []}})
        mock_request.assert_called_with('GET', 'https://api.elsevier.com/content/search/scopus', params={'query': 'AI', 'count': 25, 'start': 0, 'sort': 'coverDate', 'view': 'STANDARD'})

    @patch('scopus_mcp.client.httpx.AsyncClient.request')
    async def test_rate_limit_retry(self, mock_request):
        # First call 429, second 200
        response_429 = MagicMock()
        response_429.status_code = 429
        # Reset time in past so we don't sleep long
        response_429.headers = {'X-RateLimit-Reset': '0'} 

        response_200 = MagicMock()
        response_200.status_code = 200
        response_200.json.return_value = {'ok': True}

        mock_request.side_effect = [response_429, response_200]

        # Patch asyncio.sleep to avoid waiting
        with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            result = await self.client._request('GET', 'test')
            
            self.assertEqual(result, {'ok': True})
            self.assertEqual(mock_request.call_count, 2)
            mock_sleep.assert_called()
            
            self.assertTrue(mock_sleep.called)
            self.assertEqual(result, {'ok': True})
            self.assertEqual(mock_request.call_count, 2)

    @patch('scopus_mcp.client.httpx.AsyncClient.request')
    async def test_401_raises_auth_error_caught_by_base(self, mock_request):
        """HTTP 401 must raise ScopusAuthError, and `except ScopusAccessError`
        must still catch it (backwards compatibility)."""
        body = {"service-error": {"status": {
            "statusCode": "AUTHENTICATION_ERROR",
            "statusText": "Client IP Address: not authorized.",
        }}}
        response = MagicMock(spec=httpx.Response)
        response.status_code = 401
        response.headers = httpx.Headers({})
        response.json.return_value = body
        # raise_for_status must surface an HTTPStatusError on 4xx
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401", request=MagicMock(), response=response,
        )
        mock_request.return_value = response

        with self.assertRaises(ScopusAuthError) as ctx:
            await self.client._request('GET', 'content/search/scopus')
        # Structured attributes are populated
        self.assertEqual(ctx.exception.http_status, 401)
        self.assertEqual(ctx.exception.scopus_status_code, "AUTHENTICATION_ERROR")
        self.assertIn("not authorized", ctx.exception.scopus_status_text)
        # Backwards-compat: still catches as ScopusAccessError parent
        self.assertIsInstance(ctx.exception, ScopusAccessError)

    @patch('scopus_mcp.client.httpx.AsyncClient.request')
    async def test_403_raises_subscription_error_caught_by_base(self, mock_request):
        """HTTP 403 must raise ScopusSubscriptionError, distinct from 401."""
        body = {"service-error": {"status": {
            "statusCode": "AUTHENTICATION_ERROR",
            "statusText": "Requestor configuration settings insufficient for access to this resource.",
        }}}
        response = MagicMock(spec=httpx.Response)
        response.status_code = 403
        response.headers = httpx.Headers({})
        response.json.return_value = body
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=response,
        )
        mock_request.return_value = response

        with self.assertRaises(ScopusSubscriptionError) as ctx:
            await self.client._request('GET', 'content/abstract/citations')
        self.assertEqual(ctx.exception.http_status, 403)
        # Distinct from auth error
        self.assertNotIsInstance(ctx.exception, ScopusAuthError)
        self.assertIsInstance(ctx.exception, ScopusAccessError)


class TestBuildAdvice(unittest.TestCase):
    """Coverage for the contextual advice builder, especially the
    edge cases Codex flagged in the review (tie case + empty fallback)."""

    @staticmethod
    def _make_results(ok=0, auth=0, sub=0, not_found=0, error=0):
        out = {}
        for i in range(ok):
            out[f"ok_{i}"] = {"status": "ok"}
        for i in range(auth):
            out[f"auth_{i}"] = {"status": "auth_failed"}
        for i in range(sub):
            out[f"sub_{i}"] = {"status": "access_controlled"}
        for i in range(not_found):
            out[f"nf_{i}"] = {"status": "not_found"}
        for i in range(error):
            out[f"err_{i}"] = {"status": "error"}
        return out

    def test_tie_case_does_not_falsely_attribute(self):
        """When 401 and 403 counts are equal, advice must NOT say 'most
        failures are 401' — it must mention both fairly."""
        results = self._make_results(ok=0, auth=2, sub=2)
        advice = _build_advice(results, inst_token_present=False)
        joined = " ".join(advice).lower()
        self.assertIn("equal", joined,
                      "Tie case should explicitly call out the equality")
        self.assertNotIn("most failures are http 401", joined)

    def test_all_not_found_does_not_return_empty_advice(self):
        """All-not_found probes used to yield an empty advice list — the
        fallback now ensures callers always get at least one entry."""
        results = self._make_results(not_found=3)
        advice = _build_advice(results, inst_token_present=True)
        self.assertGreater(len(advice), 0,
                           "Advice list must never be empty even when all "
                           "probes hit 404")

    def test_all_ok_returns_positive_advice(self):
        results = self._make_results(ok=6)
        advice = _build_advice(results, inst_token_present=True)
        self.assertTrue(any("all probes passed" in a.lower() for a in advice))


if __name__ == '__main__':
    unittest.main()
