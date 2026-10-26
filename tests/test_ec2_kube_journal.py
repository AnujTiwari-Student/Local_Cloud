"""Crash windows and ownership rules for Kubernetes effects outside main state."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from localcloud.engine import Error
from localcloud.kube import KubeError
from localcloud.ec2_kube_journal import KubernetesJournal, ANNOTATION


class API:
    server='http://127.0.0.1:8001'
    def __init__(self):
        self.objects={};self.calls=[];self.serial=0;self.offline=False;self.delay_delete=False
    def get(self,path):
        if self.offline:raise KubeError('Unavailable','API unavailable',503)
        if path not in self.objects:raise KubeError('NotFound','not found',404)
        return copy.deepcopy(self.objects[path])
    def request(self,method,path,body=None):
        if self.offline:raise KubeError('Unavailable','API unavailable',503)
        self.calls.append((method,path,copy.deepcopy(body)))
        if method=='POST':
            path+='/'+body['metadata']['name']
            if path in self.objects:raise KubeError('Conflict','exists',409)
            value=copy.deepcopy(body);self.serial+=1
            value['metadata'].update(uid='uid-'+str(self.serial),resourceVersion=str(self.serial))
        elif method=='PUT':
            if path not in self.objects:raise KubeError('NotFound','not found',404)
            if body['metadata']['uid']!=self.objects[path]['metadata']['uid']:raise KubeError('Conflict','replaced',409)
            value=copy.deepcopy(body);self.serial+=1;value['metadata']['resourceVersion']=str(self.serial)
        elif method=='DELETE':
            if path not in self.objects:raise KubeError('NotFound','not found',404)
            if body['preconditions']['uid']!=self.objects[path]['metadata']['uid']:raise KubeError('Conflict','replaced',409)
            if self.delay_delete:self.objects[path]['metadata']['deletionTimestamp']='deleting'
            else:del self.objects[path]
            return {'status':'Success'}
        else:raise AssertionError(method)
        self.objects[path]=value
        return copy.deepcopy(value)


class KubeJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.filename=str(Path(self.tmp.name)/'state.db')
        self.engine=SimpleNamespace(db=sqlite3.connect(self.filename))
        self.engine.db.execute('PRAGMA journal_mode=WAL')
        self.engine.db.execute('CREATE TABLE state (body TEXT)');self.engine.db.execute("INSERT INTO state VALUES ('old')");self.engine.db.commit()
        self.api=API();self.owner='a'*32;self.labels={'localcloud.dev/runtime':self.owner,'app.kubernetes.io/managed-by':'localcloud-ec2'}
        self.adapter=SimpleNamespace(ec2=SimpleNamespace(engine=self.engine),client=self.api,namespace='test',owner=self.owner)
        self.journal=KubernetesJournal(self.adapter)
        self.collection='/api/v1/namespaces/test/configmaps';self.path=self.collection+'/guest'
        self.body={'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'guest','labels':dict(self.labels)},'data':{'value':'old'}}
    def tearDown(self):
        self.journal.close();self.engine.db.close();self.tmp.cleanup()
    def restart(self,**changes):
        self.journal.close()
        for key,value in changes.items():setattr(self.adapter,key,value)
        self.journal=KubernetesJournal(self.adapter)
        return self.journal.recover()
    def commit(self,txid):
        with self.engine.db:
            self.engine.db.execute("UPDATE state SET body='new'")
            self.journal.mark_committed(txid)
        self.journal.end(txid)
        return self.journal.recover()
    def create_committed(self):
        txid=self.journal.begin();obj=self.journal.create(self.collection,self.body,self.labels);self.commit(txid);return obj
    def test_create_intent_survives_post_response_crash_and_undoes_only_ours(self):
        txid=self.journal.begin()
        # Simulate death after the server accepted POST but before its response
        # and UID could be recorded by the client.
        job=self.journal._record('create',self.path,self.labels,desired=self.body)
        self.api.request('POST',self.collection,job['desired'])
        self.api.objects[self.collection+'/foreign']={'metadata':{'name':'foreign','uid':'foreign','labels':{'owner':'somebody-else'}}}
        report=self.restart()
        self.assertEqual(report['pending'],0);self.assertNotIn(self.path,self.api.objects)
        self.assertIn(self.collection+'/foreign',self.api.objects)
    def test_committed_create_survives_crash_and_marker_is_pruned(self):
        txid=self.journal.begin();self.journal.create(self.collection,self.body,self.labels)
        with self.engine.db:
            self.engine.db.execute("UPDATE state SET body='new'")
            self.journal.mark_committed(txid)
        report=self.restart()
        self.assertEqual(report['pending'],0);self.assertIn(self.path,self.api.objects)
        self.assertEqual(self.engine.db.execute('SELECT * FROM ec2_kube_commits').fetchall(),[])
    def test_main_rollback_prevents_marker_and_removes_uncommitted_create(self):
        txid=self.journal.begin()
        with self.assertRaises(RuntimeError):
            with self.engine.db:
                self.journal.create(self.collection,self.body,self.labels)
                self.engine.db.execute("UPDATE state SET body='new'")
                self.journal.mark_committed(txid)
                raise RuntimeError('transaction rolled back')
        self.journal.end(txid);report=self.journal.recover()
        self.assertEqual(report['pending'],0);self.assertNotIn(self.path,self.api.objects)
        self.assertEqual(self.engine.db.execute('SELECT body FROM state').fetchone()[0],'old')
    def test_uncommitted_delete_never_touches_the_resource(self):
        obj=self.create_committed();self.api.calls.clear()
        txid=self.journal.begin();self.journal.delete(self.path,obj,self.labels)
        self.assertFalse(self.api.calls)
        self.journal.end(txid);self.journal.recover()
        self.assertIn(self.path,self.api.objects);self.assertFalse(self.api.calls)
    def test_delete_runs_only_after_commit_and_waits_for_actual_absence(self):
        obj=self.create_committed();self.api.calls.clear();self.api.delay_delete=True
        txid=self.journal.begin();self.journal.delete(self.path,obj,self.labels)
        self.assertIn(self.path,self.api.objects);self.assertFalse(self.api.calls)
        report=self.commit(txid)
        self.assertEqual(report['pending'],1);self.assertTrue(report['waiting'])
        self.assertEqual(self.api.calls[-1][0],'DELETE')
        del self.api.objects[self.path]
        report=self.journal.recover();self.assertEqual(report['pending'],0)
    def test_replacement_uid_is_preserved_and_retains_pending_diagnostics(self):
        obj=self.create_committed();txid=self.journal.begin();self.journal.delete(self.path,obj,self.labels)
        self.api.objects[self.path]['metadata']['uid']='replacement'
        report=self.commit(txid)
        self.assertEqual(report['pending'],1);self.assertTrue(report['errors'])
        self.assertEqual(self.api.objects[self.path]['metadata']['uid'],'replacement')
    def test_uncommitted_update_restores_changed_fields_and_preserves_unrelated_changes(self):
        obj=self.create_committed();txid=self.journal.begin();desired=copy.deepcopy(obj);desired['data']['value']='new'
        self.journal.update(self.path,obj,desired,self.labels)
        self.api.objects[self.path]['metadata']['labels']['external']='preserved'
        self.api.objects[self.path]['data']['external']='also preserved'
        self.journal.end(txid);report=self.journal.recover()
        self.assertEqual(report['pending'],0)
        self.assertEqual(self.api.objects[self.path]['data'],{'value':'old','external':'also preserved'})
        self.assertEqual(self.api.objects[self.path]['metadata']['labels']['external'],'preserved')
    def test_committed_update_is_not_undone_after_restart(self):
        obj=self.create_committed();txid=self.journal.begin();desired=copy.deepcopy(obj);desired['data']['value']='committed'
        self.journal.update(self.path,obj,desired,self.labels)
        with self.engine.db:
            self.engine.db.execute("UPDATE state SET body='new'")
            self.journal.mark_committed(txid)
        report=self.restart()
        self.assertEqual(report['pending'],0)
        self.assertEqual(self.api.objects[self.path]['data']['value'],'committed')
    def test_ambiguous_create_does_not_remove_an_existing_foreign_resource(self):
        txid=self.journal.begin()
        self.journal._record('create',self.path,self.labels,desired=self.body)
        foreign=copy.deepcopy(self.body);foreign['metadata']['labels']={'owner':'foreign'}
        self.api.request('POST',self.collection,foreign)
        self.journal.end(txid);self.api.calls.clear()
        report=self.journal.recover()
        self.assertEqual(report['pending'],1);self.assertTrue(report['errors'])
        self.assertEqual(self.api.objects[self.path]['metadata']['labels'],{'owner':'foreign'})
        self.assertFalse(any(call[0]=='DELETE' for call in self.api.calls))
    def test_concurrent_change_to_our_field_withholds_rollback(self):
        obj=self.create_committed();txid=self.journal.begin();desired=copy.deepcopy(obj);desired['data']['value']='new'
        self.journal.update(self.path,obj,desired,self.labels)
        self.api.objects[self.path]['data']['value']='external latest'
        self.journal.end(txid);report=self.journal.recover()
        self.assertEqual(report['pending'],1);self.assertTrue(report['errors'])
        self.assertEqual(self.api.objects[self.path]['data']['value'],'external latest')
    def test_repeated_updates_to_same_object_rollback_in_reverse_order(self):
        obj=self.create_committed();txid=self.journal.begin()
        for value in ('second','third'):
            before=self.api.get(self.path);desired=copy.deepcopy(before);desired['data']['value']=value
            self.journal.update(self.path,before,desired,self.labels)
        self.journal.end(txid);report=self.journal.recover()
        self.assertEqual(report['pending'],0);self.assertEqual(self.api.objects[self.path]['data']['value'],'old')
    def test_api_unavailable_keeps_intent_for_retry(self):
        txid=self.journal.begin();self.journal.create(self.collection,self.body,self.labels);self.journal.end(txid)
        self.api.offline=True;report=self.journal.recover()
        self.assertEqual(report['pending'],1);self.assertTrue(report['errors'])
        self.api.offline=False;report=self.journal.recover();self.assertEqual(report['pending'],0)
    def test_namespace_or_endpoint_change_does_not_contact_the_other_target(self):
        txid=self.journal.begin();self.journal.create(self.collection,self.body,self.labels);self.api.calls.clear()
        report=self.restart(namespace='other')
        self.assertEqual(report['pending'],1);self.assertTrue(report['errors']);self.assertFalse(self.api.calls)
        report=self.restart(namespace='test');self.assertEqual(report['pending'],0)
    def test_second_live_session_cannot_clean_an_active_operation(self):
        txid=self.journal.begin();self.journal.create(self.collection,self.body,self.labels)
        other=KubernetesJournal(self.adapter)
        try:
            report=other.recover();self.assertEqual(report['skipped_active'],1)
            self.assertIn(self.path,self.api.objects)
        finally:other.close()
        self.journal.end(txid);self.journal.recover();self.assertNotIn(self.path,self.api.objects)
    def test_sidecar_writes_do_not_deadlock_main_sqlite_writer(self):
        txid=self.journal.begin()
        with self.engine.db:
            self.engine.db.execute("UPDATE state SET body='holding a writer transaction'")
            self.journal.create(self.collection,self.body,self.labels)
            observer=sqlite3.connect(self.journal.path)
            try:self.assertEqual(observer.execute('SELECT count(*) FROM operations').fetchone()[0],1)
            finally:observer.close()
            self.journal.mark_committed(txid)
        self.journal.end(txid);self.journal.recover()
        self.assertIn(self.path,self.api.objects)
    def test_read_only_transaction_creates_no_marker(self):
        txid=self.journal.begin()
        with self.engine.db:self.journal.mark_committed(txid)
        self.journal.end(txid)
        self.assertEqual(self.engine.db.execute('SELECT * FROM ec2_kube_commits').fetchall(),[])
    def test_memory_mode_reports_nondurable(self):
        engine=SimpleNamespace(db=sqlite3.connect(':memory:'))
        adapter=SimpleNamespace(ec2=SimpleNamespace(engine=engine),client=self.api,namespace='test',owner=self.owner)
        journal=KubernetesJournal(adapter)
        try:self.assertFalse(journal.durable)
        finally:journal.close();engine.db.close()


if __name__=='__main__':unittest.main()
