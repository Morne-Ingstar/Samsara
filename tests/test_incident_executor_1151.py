"""Diagnostics cannot veto capture/delivery. Synthetic PCM and delivery only."""
import json
import logging
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import hold_diagnostics, hold_recording_recovery as recovery
from samsara.runtime import thread_registry
from tests.test_hold_decoder_1052d import configure, segment
from tests.test_never_withhold_dictation_1125 import wire_delivery
from tests.test_silent_hold_387 import make_worker


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, 'incident_folder', lambda: tmp_path / 'holds')
    owners = []

    def attach(owner=None):
        if owner is None:
            owner = NS(config={}, model_rate=16000, _running=True)
        owner.config['diagnostics'] = {'keep_incident_holds': True}
        owner._incident_first_1052a = True
        owners.append(owner)
        return owner, recovery._store(owner)

    yield attach
    for owner in owners:
        store = getattr(owner, '_incident_store_1052a', None)
        if store is not None:
            expiry = getattr(store, 'expiry', None)
            if expiry is not None:
                expiry.cancel()
            if store.worker is not None:
                store.worker.shutdown(wait=True, cancel_futures=True)
    monkeypatch.setattr(hold_diagnostics, '_active', None)


def record(store, index=0):
    return dict(generation=store.generation, audio=np.full(1600, .04, np.float32),
                metadata=dict(timestamp=f'2026-01-01T00:00:{index:02d}+00:00',
                              reasons=['first_hold']))


def drain(store):
    store.worker.submit(lambda: None).result(timeout=5)


def warnings(caplog):
    return [r for r in caplog.records if r.name == recovery.logger.name
            and r.levelno >= logging.WARNING]


@pytest.mark.parametrize('shutdown', ['pool', 'interpreter'])
def test_shutdown_then_press_still_starts_decodes_and_delivers(
        stores, make_worker, monkeypatch, caplog, shutdown):
    worker, fake, pcm = configure(make_worker, monkeypatch, 2)
    session = wire_delivery(worker, fake, monkeypatch)
    owner, store = stores(session.app)
    previous = NS(app=owner, _decode_audio=pcm)
    recovery.begin_incident(previous)
    recovery.incident_released(previous)
    recovery.complete_incident(previous, 'Previous synthetic sentence.')
    pending = store.previous
    assert pending and not pending.get('submitted')
    old = store.worker
    old.shutdown(wait=True)
    if shutdown == 'interpreter':
        # Do not call _python_exit(): it would retire pytest's own pools too.
        monkeypatch.setattr(recovery, '_futures_thread', NS(_shutdown=True))
    session._worker = worker
    monkeypatch.setattr(worker, 'start', Mock())
    monkeypatch.setattr(thread_registry, 'register', Mock())
    owner.model.transcribe.side_effect = lambda *a, **kw: (
        iter([segment('Synthetic delivered sentence.')]), NS(language='en'))

    session.start()
    assert session._state == session.STATE_STREAMING
    worker.start.assert_called_once_with()
    session._overlay.show.assert_called_once_with()
    assert not session.cancel_event.is_set()
    session.finalize()
    worker._final_pass()
    session._paste_with_retry.assert_called_once_with('Synthetic delivered sentence.')
    assert not session.cancel_event.is_set()
    assert owner.model.transcribe.call_count == 1
    if shutdown == 'pool':
        assert store.worker is not old
        drain(store)
        assert pending['path'].exists()
        assert len(warnings(caplog)) == 1
    else:
        assert store.worker is old
        assert not pending.get('submitted')


@pytest.mark.parametrize('operation', ['write', 'refresh', 'clear'])
def test_every_archive_submission_recovers_a_retired_pool(stores, operation, caplog):
    _, store = stores()
    item = record(store)
    if operation != 'write':
        store.submit(item).result(timeout=5)
    old = store.worker
    old.shutdown(wait=True)
    if operation == 'refresh':
        item['metadata']['reasons'].append('followed_by_hold')
    future = store.clear() if operation == 'clear' else store.submit(item)
    future.result(timeout=5)
    assert store.worker is not old
    if operation == 'clear':
        assert not list(store.folder.glob('hold_*'))
    else:
        assert json.loads(item['path'].read_text())['reasons'] == item['metadata']['reasons']
    assert [r.getMessage() for r in warnings(caplog)] == ['[INCIDENT] observation_failed=1']
    assert all(r.exc_info is None for r in warnings(caplog))


def test_concurrent_submissions_create_one_replacement(stores, monkeypatch):
    _, store = stores()
    store.worker.shutdown(wait=True)
    factory = Mock(wraps=store._new_worker)
    monkeypatch.setattr(store, '_new_worker', factory)
    barrier = threading.Barrier(6)
    futures, errors = [], []

    def submit(index):
        try:
            barrier.wait(timeout=5)
            futures.append(store.submit(record(store, index)))
        except BaseException as error:
            errors.append(type(error).__name__)

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert not errors
    assert len(futures) == 6
    for future in futures:
        future.result(timeout=5)
    assert factory.call_count == 1
    assert len(list(store.folder.glob('*.json'))) == 5


def test_shutdown_racing_with_submit_is_retried_once(stores, monkeypatch):
    _, store = stores()
    old = store.worker
    submit = old.submit

    def retire(*args, **kwargs):
        old.shutdown(wait=False)
        return submit(*args, **kwargs)

    monkeypatch.setattr(old, 'submit', retire)
    item = record(store)
    store.submit(item).result(timeout=5)
    assert store.worker is not old
    assert item['path'].exists()


def test_replacement_cannot_overlap_retired_writer(stores, monkeypatch):
    _, store = stores()
    entered, release, replacement_started = (threading.Event() for _ in range(3))
    original = store._write

    def paused(*args):
        entered.set()
        assert release.wait(5)
        original(*args)

    monkeypatch.setattr(store, '_write', paused)
    first = store.submit(record(store))
    assert entered.wait(5)
    old = store.worker
    old.shutdown(wait=False)
    monkeypatch.setattr(store, '_write', original)
    try:
        second = store._submit(replacement_started.set)
        assert store.worker is not old
        # The replacement exists but cannot run its job before the old writer.
        assert not replacement_started.wait(.05)
    finally:
        release.set()
        old.shutdown(wait=True)
    first.result(timeout=5)
    second.result(timeout=5)
    assert replacement_started.is_set()


@pytest.mark.parametrize('failure', [RuntimeError, MemoryError])
def test_failed_submit_is_best_effort_and_does_not_poison_record(stores, monkeypatch, caplog, failure):
    _, store = stores()
    item = record(store)
    with monkeypatch.context() as patch:
        patch.setattr(store.worker, 'submit', Mock(side_effect=failure('Synthetic failure')))
        assert store.submit(item) is None
    assert not item.get('submitted')
    assert len(warnings(caplog)) == 1
    assert warnings(caplog)[0].getMessage() == '[INCIDENT] observation_failed=1'
    store.submit(item).result(timeout=5)
    assert item['path'].exists()


def test_failed_replacement_logs_once_and_retries_on_later_hold(stores, monkeypatch, caplog):
    _, store = stores()
    store.worker.shutdown(wait=True)
    item = record(store)
    with monkeypatch.context() as patch:
        patch.setattr(store, '_new_worker', Mock(side_effect=MemoryError))
        assert store.submit(item) is None
    assert not item.get('submitted')
    assert len(warnings(caplog)) == 1
    store.submit(item).result(timeout=5)
    assert item['path'].exists()


@pytest.mark.parametrize('exit_state', ['app', 'futures', 'threading', 'finalizing'])
def test_exit_never_constructs_or_recreates_pool(stores, monkeypatch, exit_state):
    owner, store = stores()
    old = store.worker
    old.shutdown(wait=True)
    factory = Mock(side_effect=AssertionError('No pool creation during exit'))
    monkeypatch.setattr(recovery, 'ThreadPoolExecutor', factory)
    if exit_state == 'app':
        owner._running = False
    elif exit_state == 'futures':
        monkeypatch.setattr(recovery, '_futures_thread', NS(_shutdown=True))
    elif exit_state == 'threading':
        monkeypatch.setattr(threading, '_SHUTTING_DOWN', True)
    else:
        monkeypatch.setattr(recovery.sys, 'is_finalizing', lambda: True)
    assert store.submit(record(store)) is None
    assert store.clear() is None
    assert store.worker is old
    hold = NS(app=owner)
    recovery.begin_incident(hold)
    assert not hasattr(hold, '_incident_1052a')
    del owner._incident_store_1052a
    try:
        recovery.begin_incident(NS(app=owner))
        assert not hasattr(owner, '_incident_store_1052a')
        factory.assert_not_called()
    finally:
        owner._incident_store_1052a = store


def test_exit_between_failed_submit_and_recovery_never_recreates(stores, monkeypatch):
    owner, store = stores()
    old = store.worker
    submit = old.submit

    def exit_during_submit(*args, **kwargs):
        old.shutdown(wait=False)
        owner._running = False
        return submit(*args, **kwargs)

    monkeypatch.setattr(old, 'submit', exit_during_submit)
    factory = Mock(side_effect=AssertionError('No recovery after exit'))
    monkeypatch.setattr(store, '_new_worker', factory)
    assert store.submit(record(store)) is None
    factory.assert_not_called()


@pytest.mark.parametrize('boundary', ['bind', 'store', 'serialization'])
def test_begin_incident_contains_setup_and_metadata_failures(stores, monkeypatch, caplog, boundary):
    owner, store = stores()
    hold = NS(app=owner)
    if boundary == 'bind':
        monkeypatch.setattr(hold_diagnostics, 'bind', Mock(side_effect=MemoryError))
    elif boundary == 'store':
        monkeypatch.setattr(recovery, '_store', Mock(side_effect=MemoryError))
    else:
        store.previous = record(store)
        store.previous.update(released=recovery.time.monotonic(), empty_or_failed=False)
        # 1175: fail only the product serializer, not pytest's shared JSON/watchdog.
        fake_json = NS(**vars(recovery.json))
        fake_json.dumps = Mock(side_effect=MemoryError)
        monkeypatch.setattr(recovery, 'json', fake_json)
    recovery.begin_incident(hold)
    assert len(warnings(caplog)) == 1


@pytest.mark.parametrize('boundary', ['complete', 'update'])
def test_final_archive_failures_leave_final_result_usable(stores, monkeypatch, caplog, boundary):
    owner, store = stores()
    hold = NS(app=owner, _decode_audio=np.full(1600, .04, np.float32))
    recovery.begin_incident(hold)
    if boundary == 'update':
        recovery.complete_incident(hold, 'Synthetic sentence.')
        hold._incident_record_1121 = record(store)
    monkeypatch.setattr(recovery, '_store', Mock(side_effect=MemoryError))
    if boundary == 'complete':
        assert recovery.prepare_final(hold, 'Synthetic sentence.', 'Synthetic sentence.', .1) == (
            'Synthetic sentence.', 'Synthetic sentence.', .1)
    else:
        recovery.update_incident(hold)
    assert len(warnings(caplog)) == 1


def test_warning_failure_cannot_escape_begin_or_submit(stores, monkeypatch):
    owner, store = stores()
    monkeypatch.setattr(recovery.logger, 'warning', Mock(side_effect=MemoryError))
    monkeypatch.setattr(store.worker, 'submit', Mock(side_effect=MemoryError))
    assert store.submit(record(store)) is None
    monkeypatch.setattr(hold_diagnostics, 'bind', Mock(side_effect=MemoryError))
    recovery.begin_incident(NS(app=owner))


def test_clear_generation_survives_replacement(stores):
    _, store = stores()
    stale = record(store)
    store.worker.shutdown(wait=True)
    store.clear().result(timeout=5)
    assert store.submit(stale) is None
    assert not stale.get('submitted')
    fresh = record(store)
    store.submit(fresh).result(timeout=5)
    assert len(list(store.folder.glob('*.json'))) == 1


def test_broken_pool_is_replaced(stores):
    _, store = stores()
    old = store.worker
    old._broken = 'Synthetic initializer failure'
    item = record(store)
    store.submit(item).result(timeout=5)
    assert store.worker is not old
    assert item['path'].exists()


def test_async_clear_failure_logs_once_without_raising(stores, caplog):
    _, store = stores()
    store.folder = NS(exists=lambda: True, glob=Mock(side_effect=OSError))
    assert store.clear().result(timeout=5) is None
    assert [r.getMessage() for r in warnings(caplog)] == ['[INCIDENT] observation_failed=1']
