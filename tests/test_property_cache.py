"""Cross-search evidence reuse, freshness, context safety and deletion retention."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import json
import sqlite3

import pandas as pd

from src import airbnb_service as service, property_cache, search_catalog, search_service, storage_service
from src.request_policy import RequestPolicy, automatic_options
from tests.test_airbnb_service import FakeProvider, setup_search


def _detail(definition, stamp=None):
    return {'room_id':'1','name':'Flat','total_price':500,'price':{'total':500},
        'details_fetched_at':stamp or datetime.now(timezone.utc).isoformat(),
        'trip_revision':definition['trip_revision'],'quote_attempt_status':'success',
        'quote_party_supported':True, **{'quote_'+k:definition[k] for k in
          ('check_in','check_out','adults','children','infants','pets','currency')}}


def test_shared_context_and_expiry_and_secret_exclusion(tmp_path):
    cache = property_cache.PropertyCache(tmp_path/'searches')
    definition = {'trip_revision':1,'check_in':'2027-01-01','check_out':'2027-01-05',
                  'adults':2,'children':0,'infants':0,'pets':0,'currency':'EUR'}
    detail = _detail(definition)
    detail.update(api_key='secret',cookies={'s':'secret'},cancellation_policy='Free until January 1')
    cache.store([detail],[],definition)
    hit = cache.load(['1'],dict(definition,trip_revision=8))['1']
    assert hit['details']['trip_revision'] == 8
    assert 'secret' not in json.dumps(hit)
    assert 'cancellation_policy' not in hit['metadata']
    for key,value in [('check_in','2027-02-01'),('check_out','2027-02-05'),('adults',3),('children',1),('currency','GBP')]:
        changed = cache.load(['1'],dict(definition,**{key:value}))['1']
        assert 'details' not in changed and 'metadata' in changed
        assert 'price' not in changed['metadata'] and 'total_price' not in changed['metadata']
    old = (datetime.now(timezone.utc)-timedelta(days=31)).isoformat()
    with sqlite3.connect(cache.path) as db:
        db.execute('UPDATE evidence SET fetched=?',(old,))
    assert cache.load(['1'],definition) == {}
    assert not (tmp_path/'searches/analyzer.sqlite3').exists()


def test_overlapping_search_after_deletion_uses_zero_provider_requests(tmp_path,monkeypatch):
    setup_search(tmp_path,monkeypatch,children=0)
    root = tmp_path/'searches'
    definition = search_service.load_search_definition('trip',root=root)
    provider = FakeProvider([{'room_id':'1','latitude':45.1,'longitude':7.7,'total_price':100}])
    policy = RequestPolicy(automatic_options())
    monkeypatch.setattr(policy,'wait',lambda *args:None)
    service.discover_listings('trip',provider=provider)
    service.collect_details_and_reviews('trip',provider=provider,request_policy=policy)
    assert len(provider.detail_calls)==len(provider.review_calls)==1
    search_catalog.delete_search('trip','trip',root=root)
    assert not (root/'trip').exists()
    assert (root/'.property_cache.sqlite3').exists()
    changed = dict(definition)
    changed['geometry']={'type':'bbox','south':45.0,'west':7.5,'north':45.3,'east':7.9}
    search_service.save_search_definition('overlap',changed,create=True,root=root)
    service.discover_listings('overlap',provider=provider)
    result = service.collect_details_and_reviews('overlap',provider=provider,request_policy=policy)
    assert result['status']=='success'
    assert result['shared_cache_details']==result['shared_cache_reviews']==1
    assert len(provider.detail_calls)==len(provider.review_calls)==1
    assert not (root/'analyzer.sqlite3').exists()
    merged = storage_service.read_frame(root/'overlap/output_data/merged_df.csv')
    assert merged.iloc[0]['name']=='Listing 1'


def test_delete_backfills_legacy_evidence_before_dropping_search_database(tmp_path,monkeypatch):
    setup_search(tmp_path,monkeypatch,children=0)
    root=tmp_path/'searches'
    definition=search_service.load_search_definition('trip',root=root)
    storage_service.write_frame(root/'trip/output_data/details_df.csv',pd.DataFrame([_detail(definition)]),export=False)
    search_catalog.delete_search('trip','trip',root=root)
    assert not (root/'trip/analyzer.sqlite3').exists()
    assert property_cache.PropertyCache(root).load(['1'],definition)['1']['details']['total_price']==500


def test_changed_stay_reuses_metadata_and_reviews_but_gets_new_price(tmp_path,monkeypatch):
    setup_search(tmp_path,monkeypatch,children=0)
    root=tmp_path/'searches'
    definition=search_service.load_search_definition('trip',root=root)
    cache=property_cache.PropertyCache(root)
    stamp=datetime.now(timezone.utc).isoformat()
    review={'room_id':'1','reviews_fetched_at':stamp,'reviews_json':json.dumps([{'id':'r','comments':'Quiet'}]),'reviews_text':'Quiet'}
    cache.store([_detail(definition)],[review],definition)
    changed=dict(definition,check_in=(date.fromisoformat(definition['check_in'])+timedelta(days=7)).isoformat(),
                 check_out=(date.fromisoformat(definition['check_out'])+timedelta(days=7)).isoformat())
    search_service.save_search_definition('later',changed,create=True,root=root)
    provider=FakeProvider([{'room_id':'1','latitude':45.1,'longitude':7.7,'total_price':100}])
    service.discover_listings('later',provider=provider)
    provider.__name__='pyairbnb'
    def forbidden(*args,**kwargs):
        raise AssertionError('Cached metadata and reviews must not be refetched')
    provider.details=SimpleNamespace(get=forbidden)
    provider.get_reviews=forbidden
    provider.get_api_key=lambda *args:'fresh-key'
    calls=[]
    def price(**kwargs):
        calls.append(kwargs)
        return {'total':700}
    provider.get_price=price
    policy=RequestPolicy(automatic_options())
    monkeypatch.setattr(policy,'wait',lambda *args:None)
    result=service.collect_details_and_reviews('later',provider=provider,request_policy=policy)
    assert result['status']=='success'
    assert len(calls)==1 and calls[0]['check_in'].isoformat()==changed['check_in']
    assert result['shared_cache_reviews']==1
    saved=storage_service.read_frame(root/'later/output_data/details_df.csv')
    assert saved.iloc[0]['price']['total']==700


def test_older_evidence_is_retained_but_never_automatically_reused(tmp_path):
    cache=property_cache.PropertyCache(tmp_path/'searches')
    definition={'trip_revision':1,'check_in':'2027-01-01','check_out':'2027-01-05',
                'adults':2,'children':0,'infants':0,'pets':0,'currency':'EUR'}
    old=(datetime.now(timezone.utc)-timedelta(days=45)).isoformat()
    cache.store([_detail(definition,old)],[{'room_id':'1','reviews_fetched_at':old,'reviews_text':'Old evidence'}],definition)
    assert cache.load(['1'],definition)=={}
    with sqlite3.connect(cache.path) as db:
        assert db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]==3
