"""813: hold-to-record streaming decodes carry no vocabulary prompt."""
from types import SimpleNamespace

from samsara import hold_decode_params
from samsara.streaming import StreamingWorker

VOCAB = "Common terms: sol, calculator"


class _Store:
    def get_initial_prompt(self, include_vocabulary=True):
        return None if not include_vocabulary else VOCAB


class _App:
    config = {}
    model_rate = 16000

    def get_transcription_params(self, include_vocabulary=True):
        return {'language': 'en', 'beam_size': 3, 'temperature': 0.0,
                'initial_prompt': None if not include_vocabulary else VOCAB}


def _worker(monkeypatch):
    import samsara.dictionary_store as ds
    monkeypatch.setattr(ds, 'get_dictionary_store', lambda app: _Store())
    w = StreamingWorker.__new__(StreamingWorker)
    w._session = SimpleNamespace(app=_App())
    return w


def test_partial_prompt_has_no_vocabulary(monkeypatch):
    assert _worker(monkeypatch)._partial_params()['initial_prompt'] is None


def test_final_prompt_has_no_vocabulary(monkeypatch):
    assert _worker(monkeypatch)._final_params()['initial_prompt'] is None


def test_explicit_prompt_override_kept(monkeypatch):
    class Store(_Store):
        def get_initial_prompt(self, include_vocabulary=True):
            return 'mine' if not include_vocabulary else 'mine ' + VOCAB
    import samsara.dictionary_store as ds
    monkeypatch.setattr(ds, 'get_dictionary_store', lambda app: Store())
    assert hold_decode_params.free_form_prompt(_App()) == 'mine'


def test_prompt_failure_falls_back_to_none(monkeypatch):
    import samsara.dictionary_store as ds

    def boom(app):
        raise RuntimeError('x')
    monkeypatch.setattr(ds, 'get_dictionary_store', boom)
    assert hold_decode_params.free_form_prompt(_App()) is None


def test_final_params_failure_fallback():
    class Bad:
        def get_transcription_params(self, include_vocabulary=True):
            raise RuntimeError('x')
    assert hold_decode_params.final_base_params(Bad(), 'en') == {'language': 'en', 'initial_prompt': None}
