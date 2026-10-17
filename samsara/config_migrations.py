"""Legacy config migrations behind the DictationApp compatibility facade.

The host supplies config, save_config(), _wake_word_config_already_migrated()
and _deep_update(target, source). The caller holds the existing config lock;
these helpers never acquire it. Saves and callbacks stay on the host so their
order, overrides and migration-save behavior remain authoritative there.
"""

import copy
import logging

from samsara import wake_profiles

logger = logging.getLogger("Samsara")


def _migrate_wake_word_config(self, default_config):
    """Migrate old flat wake word settings to new nested structure"""
    # The top-level key is the sole listener switch.  The stale nested
    # key is never read, so remove it.  Only use its legacy value when
    # the authoritative key is absent.
    wwc = self.config.get('wake_word_config')
    if isinstance(wwc, dict) and 'enabled' in wwc:
        legacy_enabled = wwc.pop('enabled')
        if 'wake_word_enabled' not in self.config:
            self.config['wake_word_enabled'] = bool(legacy_enabled)
        self.save_config()
    if self._wake_word_config_already_migrated():
        wake_profiles.validate_wake_profiles(self.config['wake_profiles'])
        logger.debug("wake config migration: no-op")
        return

    # wake_word_config already existing on entry means the branch below
    # that creates it (and saves) won't run -- if the only change this
    # pass makes is dropping a stale wake_targets block, nothing else
    # persists it, so save explicitly at the end. See the wake_targets
    # branch just below.
    _wwc_present_at_entry = isinstance(self.config.get('wake_word_config'), dict)
    _removed_stale_wake_targets = False

    # Migrate old wake_word/combined modes to wake_word_enabled + hold
    old_mode = self.config.get('mode')
    if old_mode in ('wake_word', 'combined'):
        self.config['wake_word_enabled'] = True
        self.config['mode'] = 'hold'
        logger.info(f"[MIGRATE] mode='{old_mode}' -> mode='hold' + wake_word_enabled=True")

    # Multi-wakeword: wake_targets -> wake_profiles rename (tribunal spec,
    # design review arc_20260629_170545.md). Renames an existing on-disk
    # list in place rather than discarding the user's own edits. If both
    # keys are present, wake_profiles is authoritative -- wake_targets is
    # an orphaned leftover from a rename that ran before wake_profiles
    # existed; drop it rather than merge its (stale) contents.
    if 'wake_targets' in self.config:
        if 'wake_profiles' not in self.config:
            self.config['wake_profiles'] = self.config.pop('wake_targets')
            logger.info("[MIGRATE] Renamed wake_targets -> wake_profiles")
        else:
            del self.config['wake_targets']
            logger.info("[MIGRATE] Removed stale wake_targets (wake_profiles is authoritative)")
            _removed_stale_wake_targets = True

    if 'wake_profiles' not in self.config:
        self.config['wake_profiles'] = copy.deepcopy(default_config.get('wake_profiles', []))
        logger.info("[MIGRATE] Injected default wake_profiles")

    _wwc = self.config.get('wake_word_config')
    _renamed_cancel_words = False
    if isinstance(_wwc, dict) and 'cancel_words' in _wwc:
        _wwc.setdefault('wake_abort_phrase', _wwc['cancel_words'])
        del _wwc['cancel_words']
        _renamed_cancel_words = True
        logger.info("[MIGRATE] Removed wake_word_config.cancel_words after preserving wake_abort_phrase")

    # Check if we have old flat config but no new nested config
    if 'wake_word_config' not in self.config:
        # Create new nested config from defaults (deep copy so nested
        # dicts are not shared with default_config)
        import copy as _copy
        self.config['wake_word_config'] = _copy.deepcopy(default_config['wake_word_config'])

        # Migrate old values if they exist
        if 'wake_word' in self.config:
            self.config['wake_word_config']['phrase'] = self.config['wake_word']
        if 'wake_word_timeout' in self.config:
            # Old flat timeout. The 'modes' nesting no longer exists in the
            # schema; only apply if the current default actually has that
            # path, otherwise drop it silently (schema moved on).
            _wwc = self.config['wake_word_config']
            _modes = _wwc.get('modes')
            if isinstance(_modes, dict) and isinstance(_modes.get('dictate'), dict):
                _modes['dictate']['silence_timeout'] = self.config['wake_word_timeout']
        if 'min_speech_duration' in self.config:
            self.config['wake_word_config']['audio']['min_speech_duration'] = self.config['min_speech_duration']

        # Save migrated config — _config_lock is already held by the
        # load_config() caller, so do not re-acquire (threading.Lock is
        # not reentrant and would deadlock).
        self.save_config()
        logger.info("[CONFIG] Migrated wake word settings to new format")
    else:
        # Ensure all nested keys exist (for configs created between versions)
        self._deep_update(self.config['wake_word_config'], default_config['wake_word_config'])

    _wwc = self.config['wake_word_config']

    # Per-profile send_word (agentic-safety requirement): migrate the old
    # GLOBAL send_words list (wake_word_config.send_words, first entry)
    # into each profile that doesn't already carry its own send_word, and
    # fold legacy send_policy ('enter'|'stage_only') into mode
    # ('focus_dictate'|'stage_send'). A stage_send (agentic) target must
    # end up with a send_word distinct from any focus_dictate target's --
    # this only fills a default, it never clobbers an explicit value, so
    # profiles can diverge from here on.
    _legacy_send_words = _wwc.get('send_words') or ['over']
    _legacy_default_send_word = _legacy_send_words[0]
    for _profile in self.config['wake_profiles']:
        if isinstance(_profile, dict):
            wake_profiles.normalize_profile_mode_and_send_word(
                _profile, default_send_word=_legacy_default_send_word)

    wake_profiles.validate_wake_profiles(self.config['wake_profiles'])

    # Persist the stale wake_targets removal so it doesn't reappear next
    # boot. When wake_word_config didn't exist at entry, the branch above
    # already called save_config() and this same removal rode along with
    # it -- only save again here for the steady-state case where nothing
    # else in this pass triggered a save.
    if _renamed_cancel_words or (_removed_stale_wake_targets and _wwc_present_at_entry):
        self.save_config()

def _wake_word_config_already_migrated(self) -> bool:
    """True when every migration this function performs would be a
    no-op against the current in-memory config: all target keys are
    already in place and no legacy source key survives. Lets
    _migrate_wake_word_config() skip the (redundant, every-boot) rename
    checks and wake_word_config deep-merge once a config has settled.
    """
    wwc = self.config.get('wake_word_config')
    if not isinstance(wwc, dict):
        return False
    if self.config.get('mode') in ('wake_word', 'combined'):
        return False
    if 'wake_targets' in self.config or 'wake_profiles' not in self.config:
        return False
    if 'wake_word' in self.config or 'wake_word_timeout' in self.config \
            or 'min_speech_duration' in self.config:
        return False
    if 'cancel_words' in wwc:
        return False
    profiles = self.config.get('wake_profiles')
    if not isinstance(profiles, list):
        return False
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        if 'send_policy' in profile or 'mode' not in profile or 'send_word' not in profile:
            return False
    return True

def _migrate_recording_media_mode(self):
    """Fold the legacy recording-duck switches into one lane policy."""
    ducking_cfg = self.config.setdefault('ducking', {})
    if 'recording_mode' in ducking_cfg:
        return
    hold_ducks = bool(self.config.get('capture_duck_hold_enabled', True))
    ordinary_ducks = bool(ducking_cfg.get('enabled', False))
    ducking_cfg['recording_mode'] = 'duck' if hold_ducks or ordinary_ducks else 'off'
    self.save_config()
    logger.info(
        '[MIGRATE] recording media: hold_duck=%s ordinary_duck=%s -> %s',
        hold_ducks, ordinary_ducks, ducking_cfg['recording_mode'],
    )

def _migrate_command_matching_enabled_flag(self):
    """Migrate the legacy top-level 'command_mode_enabled' flag into
    'command_mode.command_matching_enabled'.

    Two near-identically-named flags coexisted: top-level
    'command_mode_enabled' (legacy -- gates command MATCHING during
    regular dictation) and nested 'command_mode.enabled' (gates the
    walkie-talkie button/session listener, unrelated). Renaming the
    legacy one to live inside the same 'command_mode' sub-dict, next to
    its actual sibling settings, makes the two impossible to confuse by
    name alone. Runs once per legacy config; a config that never had the
    top-level key (fresh installs, already-migrated configs) is a no-op.
    """
    if 'command_mode_enabled' not in self.config:
        return
    legacy_value = self.config.pop('command_mode_enabled')
    self.config.setdefault('command_mode', {}).setdefault(
        'command_matching_enabled', legacy_value)
    self.save_config()
    logger.info(
        "[MIGRATE] Removed command_mode_enabled after preserving "
        "command_mode.command_matching_enabled"
    )

def _migrate_ai_command_mode_config(self):
    """Migrate the deleted 'ai_command_mode' config block to
    'ava_command_session' (Ava Front Door spec v2 "Migration" section
    -- D3 replaces samsara/ai_command_mode.py, which no longer
    exists). Runs once per legacy config; a config that never had the
    top-level key (fresh installs, already-migrated configs) is a
    no-op.

    Key mapping -- meaningful keys carried over as-is:
      enabled, key, backend, model, queue_depth_cap, keep_warm,
      miss_limit, ready_cue_enabled, ready_cue_dir.
    Dead keys dropped (not straight-renamed -- each was tied to
    machinery this pass deletes, per spec's DROP list):
      wake_phrase       -- D2's exact-phrase Ava entry covers voice
                           entry now; D3 is key-only.
      step_settle_seconds, show_plan_hud
                        -- the old multi-step "plan" HUD/executor is
                           gone; the new waterfall resolves to at
                           most one dispatched action per utterance
                           via the app's ordinary command dispatch.
      menu_limit        -- replaced by shortlist_size (a per-utterance
                           fuzzy top-N, not a flat truncation of the
                           whole menu) -- not a value-preserving
                           rename, so not migrated.
    """
    if 'ai_command_mode' not in self.config:
        return
    old = self.config.pop('ai_command_mode') or {}
    carried = {
        k: old[k] for k in (
            'enabled', 'key', 'backend', 'model', 'queue_depth_cap',
            'keep_warm', 'miss_limit', 'ready_cue_enabled', 'ready_cue_dir',
        ) if k in old
    }
    dropped = sorted(set(old) - set(carried))
    existing = self.config.setdefault('ava_command_session', {})
    merged = {**carried, **existing}  # a fresh ava_command_session block (unlikely but possible) wins
    self.config['ava_command_session'] = merged
    self.save_config()
    logger.info(
        f"[MIGRATE] ai_command_mode -> ava_command_session: carried {sorted(carried)}, "
        f"dropped {dropped}"
    )

def _migrate_retired_ava_command_session_config(self):
    """Drop keys that only configured the retired Ava capture session.

    The section remains the stable home for Ava's provider/model and the
    opt-in COMMAND-miss interpreter. This migration is idempotent and
    writes only when an existing profile actually changes.
    """
    cfg = self.config.get('ava_command_session')
    if not isinstance(cfg, dict):
        return
    removed = ('key', 'miss_limit', 'queue_depth_cap', 'inactivity_timeout_s',
               'ready_cue_enabled', 'ready_cue_dir')
    changed = [key for key in removed if key in cfg]
    for key in changed:
        cfg.pop(key, None)
    if 'interpret_misses' not in cfg:
        cfg['interpret_misses'] = True
        changed.append('interpret_misses')
    if changed:
        self.save_config()
        logger.info('[MIGRATE] ava_command_session retired keys=%s', sorted(changed))

def _deep_update(self, target, source):
    """Recursively update target dict with missing keys from source"""
    for key, value in source.items():
        if key not in target:
            target[key] = value
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            self._deep_update(target[key], value)
