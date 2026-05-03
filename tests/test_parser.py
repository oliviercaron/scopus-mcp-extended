import pytest
from scopus_mcp.utils import clean_search_results, clean_abstract_details, clean_author_profile

def test_clean_search_results_empty():
    assert clean_search_results({}) == []
    assert clean_search_results({'search-results': {}}) == []

def test_clean_search_results_valid():
    data = {
        'search-results': {
            'entry': [
                {
                    'dc:identifier': 'SCOPUS_ID:12345',
                    'dc:title': 'Test Title',
                    'prism:coverDate': '2023-01-01',
                    'citedby-count': '10',
                    'link': [{'@ref': 'scopus', '@href': 'http://example.com'}]
                }
            ]
        }
    }
    cleaned = clean_search_results(data)
    assert len(cleaned) == 1
    assert cleaned[0]['scopus_id'] == '12345'
    assert cleaned[0]['title'] == 'Test Title'
    assert cleaned[0]['url'] == 'http://example.com'

def test_clean_abstract_details():
    data = {
        'abstracts-retrieval-response': {
            'coredata': {
                'dc:identifier': 'SCOPUS_ID:999',
                'dc:title': 'Abstract Title',
                'dc:description': 'This is an abstract.'
            },
            'authors': {
                'author': [
                    {'@auid': '111', 'ce:indexed-name': 'Doe J.'}
                ]
            }
        }
    }
    cleaned = clean_abstract_details(data)
    assert cleaned['scopus_id'] == '999'
    assert cleaned['title'] == 'Abstract Title'
    assert len(cleaned['authors']) == 1
    assert cleaned['authors'][0]['auth_id'] == '111'
