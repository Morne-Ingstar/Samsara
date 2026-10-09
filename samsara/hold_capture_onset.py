"""Bounded ordinary-hold onset policy and timing-only capture evidence."""
import threading


class HoldOnset:
    def __init__(self, token, requested_at, confirmed_at, eligible, record):
        self.token = token
        self.requested_at = requested_at
        self.policy = 'eligible' if eligible else 'strict'
        self.boundary = requested_at - 0.250 if eligible else confirmed_at
        self.confirmed_at = confirmed_at
        self.first_kept_block_start = None
        self.opening_blocks_dropped = 0
        self.rewind_suppressed = None
        self._record = record
        self._recorded = False
        self._lock = threading.Lock()

    def observe(self, start, kept, before):
        with self._lock:
            if self.first_kept_block_start is None:
                if kept:
                    self.first_kept_block_start = start
                    self._emit()
                elif before is not None and start < before:
                    self.opening_blocks_dropped += 1

    def finish(self):
        with self._lock:
            self._emit()

    def _emit(self):
        if self._recorded:
            return
        self._recorded = True
        self._record('hold_capture.onset', owner_token=self.token,
                     policy=self.policy, boundary=self.boundary,
                     first_kept_block_start=self.first_kept_block_start,
                     opening_blocks_dropped=self.opening_blocks_dropped,
                     rewind_suppressed=self.rewind_suppressed)


def open_owned_duck(app, token, audio_ducking, logger):
    """Return (owner, fresh), reserving creation atomically for this hold.

    The shared opener cannot report which caller created the ducker. Reserve
    its existing starting generation under its existing lock, then perform
    the same synchronous start/publication protocol. Reuse delegates to the
    original opener and can never qualify for pre-roll.
    """
    with app._hands_free_duck_lock:
        fresh = (app._hands_free_capture_ducker is None
                 and not app._hands_free_capture_duck_starting)
        if fresh:
            timer = app._hands_free_duck_restore_timer
            app._hands_free_duck_restore_timer = None
            if timer is not None:
                timer.cancel()
            app._hands_free_capture_duck_owners.add(token)
            app._hands_free_capture_duck_starting = True
            app._hands_free_capture_duck_start_generation += 1
            generation = app._hands_free_capture_duck_start_generation
    if not fresh:
        return app._open_hands_free_capture_duck(token), False

    ducker = None
    try:
        cfg = app.config.get('ducking', {}) or {}
        ducker = audio_ducking.SessionDucker(
            duck_level=float(cfg.get('hands_free_level', 0.15)),
            exclude_pids=app._hands_free_duck_excludes())
        ducker.start()
        with app._hands_free_duck_lock:
            current = generation == app._hands_free_capture_duck_start_generation
            if current:
                app._hands_free_capture_duck_starting = False
            if current and app._hands_free_capture_duck_owners:
                if app._hands_free_capture_ducker is None:
                    app._hands_free_capture_ducker = ducker
            published = current and app._hands_free_capture_ducker is ducker
        if not published:
            ducker.stop()
            with app._hands_free_duck_lock:
                app._hands_free_capture_duck_owners.discard(token)
            return None, False
        app._bump_wake_gate_freeze()
        app._log_duck_result(ducker, 'capture duck engaged')
        app._bump_wake_gate_freeze()
        return token, True
    except Exception as exc:
        with app._hands_free_duck_lock:
            if generation == app._hands_free_capture_duck_start_generation:
                app._hands_free_capture_duck_starting = False
            app._hands_free_capture_duck_owners.discard(token)
        logger.warning('[DUCK] Failed to engage capture duck: %s', exc)
        if ducker is not None:
            try:
                ducker.stop()
            except Exception:
                pass
        return None, False


def finish_open(app, token, started, confirmed, time, flight_recorder, logger):
    """Preserve the existing engage evidence and failed-open cleanup."""
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    flight_recorder.record('hold_capture_duck.engage', owner_token=token,
                           confirmed=confirmed, elapsed_ms=elapsed_ms)
    logger.info('[DUCK] Hold capture confirmed=%s in %.2f ms', confirmed, elapsed_ms)
    if not confirmed:
        app._close_hold_capture_duck()


def close_onset(app):
    onset, app._hold_capture_onset = getattr(app, '_hold_capture_onset', None), None
    if onset is not None:
        onset.finish()
