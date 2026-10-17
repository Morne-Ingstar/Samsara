"""Opt-in short-command decoding for finalized session audio."""

import logging

from samsara.stage_timing import current_stage_timing

logger = logging.getLogger("Samsara")


class CommandFastPathMixin:
    """Keep the session decoder available on the app without bootstrapping it."""

    def _decode_session_audio(self, audio, transcribe_params, *, command_lane):
        """Decode finalized audio; optionally try a bounded command pass first.

        A partial prefix must never trigger an action. This uses the entire
        silence-bounded recording and retains the original audio/parameters
        for fallback. DICTATE/AVA keep their original single decode. Opt-in
        until recorded speech establishes the beam-1 accuracy/latency trade.
        """
        enabled = (self.config.get('command_mode') or {}).get('short_decode_enabled', False)
        short = command_lane and enabled and len(audio) <= 4 * self.model_rate
        with self.model_lock:
            if short and transcribe_params.get('beam_size', 1) > 1:
                from samsara.command_catalog import matches_short_command
                params = dict(transcribe_params, beam_size=1, best_of=1, temperature=0.0)
                try:
                    segments, info = self.model.transcribe(audio, **params)
                    segments = list(segments)
                    text = ''.join(s.text for s in segments).strip()
                    allow_chain = (self.config.get('commands') or {}).get('grammar_tier', True) is not False
                    if matches_short_command(text, self.command_executor, allow_chain=allow_chain):
                        current_stage_timing().mark('transcribe_start', beam=1)
                        return segments, info
                except Exception as exc:
                    logger.debug('[COMMAND-FAST] falling back after %s', type(exc).__name__)
            segments, info = self.model.transcribe(audio, **transcribe_params)
            return list(segments), info
