"""
Profile Manager for Samsara
Handles saving, loading, importing, and exporting dictionary and command profiles.
"""

import json
import os
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, List

from samsara.log import get_logger

logger = get_logger(__name__)

DICTIONARY_BUNDLE_FORMAT = "samsara.dictionary"
DICTIONARY_BUNDLE_VERSION = 1
DEFAULT_COMMAND_PROFILE = "Default"


class ProfileManager:
    """Manages dictionary and command profiles for Samsara."""
    
    def __init__(self, app_dir: str, commands_path: Optional[str] = None):
        """
        Initialize the profile manager.
        
        Args:
            app_dir: The root Samsara application directory
        """
        self.app_dir = Path(app_dir)
        self.profiles_dir = self.app_dir / "profiles"
        self.dictionaries_dir = self.profiles_dir / "dictionaries"
        self.commands_dir = self.profiles_dir / "commands"
        
        # Active data file paths
        self.training_data_path = self.app_dir / "training_data.json"
        self.commands_path = Path(commands_path) if commands_path else self.app_dir / "commands.json"
        self.default_commands_path = self.app_dir / "commands.default.json"
        self.config_path = self.app_dir / "config.json"
        
        # Ensure directories exist
        self._ensure_directories()
    
    def _ensure_directories(self):
        """Create profile directories if they don't exist."""
        self.dictionaries_dir.mkdir(parents=True, exist_ok=True)
        self.commands_dir.mkdir(parents=True, exist_ok=True)
        # Capture the shipped command set once.  This is the read-only source
        # for the built-in Default profile, even after the editable command
        # file has been changed by the user.
        if not self.default_commands_path.exists() and self.commands_path.exists():
            try:
                shutil.copy2(self.commands_path, self.default_commands_path)
            except OSError as exc:
                logger.debug("Could not seed Default command profile: %s", exc)
    
    # =========================================================================
    # Dictionary Profile Methods
    # =========================================================================
    
    def list_dictionary_profiles(self) -> List[str]:
        """Get list of available dictionary profile names (without .json extension)."""
        profiles = []
        if self.dictionaries_dir.exists():
            for f in self.dictionaries_dir.glob("*.json"):
                profiles.append(f.stem)
        return sorted(profiles)
    
    def load_dictionary_profile_metadata(self, name: str) -> Optional[Dict[str, Any]]:
        """Load just the metadata from a dictionary profile (not the full data)."""
        path = self.dictionaries_dir / f"{name}.json"
        if not path.exists():
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return {
                'name': data.get('profile_name', name),
                'description': data.get('description', ''),
                'author': data.get('author', ''),
                'version': data.get('version', '1.0'),
                'created': data.get('created', ''),
                'vocab_count': len(data.get('vocabulary', [])),
                'corrections_count': len(data.get('corrections', {}))
            }
        except Exception:
            return None
    
    def save_dictionary_profile(self, name: str, description: str = "", 
                                author: str = "", overwrite: bool = False) -> Tuple[bool, str]:
        """
        Save current dictionary (vocabulary + corrections) as a named profile.
        
        Args:
            name: Profile name (will be used as filename)
            description: Optional description
            author: Optional author name
            overwrite: If True, overwrite existing profile
            
        Returns:
            Tuple of (success, message)
        """
        path = self.dictionaries_dir / f"{name}.json"
        
        if path.exists() and not overwrite:
            return False, f"Profile '{name}' already exists. Use overwrite=True to replace."
        
        # Load current training data
        try:
            with open(self.training_data_path, 'r', encoding='utf-8') as f:
                current_data = json.load(f)
        except Exception as e:
            return False, f"Could not read current dictionary: {e}"
        
        # Load initial prompt from config if available
        initial_prompt = ""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                initial_prompt = config.get('initial_prompt', '')
        except Exception as e:
            logger.debug(f"save_dictionary_profile: {e}")
        
        # Create profile structure
        profile = {
            'profile_name': name,
            'description': description,
            'author': author,
            'version': '1.0',
            'created': datetime.now().strftime('%Y-%m-%d'),
            'vocabulary': current_data.get('vocabulary', []),
            'corrections': current_data.get('corrections', {}),
            'initial_prompt': initial_prompt
        }
        
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(profile, f, indent=2, ensure_ascii=False)
            return True, f"Dictionary profile '{name}' saved successfully."
        except Exception as e:
            return False, f"Failed to save profile: {e}"
    
    def load_dictionary_profile(self, name: str, merge: bool = False) -> Tuple[bool, str]:
        """
        Load a dictionary profile, either replacing or merging with current.
        
        Args:
            name: Profile name to load
            merge: If True, merge with current data; if False, replace
            
        Returns:
            Tuple of (success, message)
        """
        path = self.dictionaries_dir / f"{name}.json"
        
        if not path.exists():
            return False, f"Profile '{name}' not found."
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                profile = json.load(f)
        except Exception as e:
            return False, f"Could not read profile: {e}"
        
        if merge:
            # Load current data and merge
            try:
                with open(self.training_data_path, 'r', encoding='utf-8') as f:
                    current = json.load(f)
            except Exception:
                current = {'vocabulary': [], 'corrections': {}}
            
            # Merge vocabulary (avoid duplicates)
            existing_vocab = set(current.get('vocabulary', []))
            new_vocab = profile.get('vocabulary', [])
            merged_vocab = list(existing_vocab | set(new_vocab))
            
            # Merge corrections (new overwrites existing for same keys)
            merged_corrections = current.get('corrections', {}).copy()
            merged_corrections.update(profile.get('corrections', {}))
            
            new_data = {
                'vocabulary': sorted(merged_vocab),
                'corrections': merged_corrections
            }
            added_vocab = len(merged_vocab) - len(existing_vocab)
            message = f"Merged profile '{name}': added {added_vocab} vocabulary items."
        else:
            # Replace completely
            new_data = {
                'vocabulary': profile.get('vocabulary', []),
                'corrections': profile.get('corrections', {})
            }
            message = f"Loaded profile '{name}' (replaced current dictionary)."
        
        try:
            with open(self.training_data_path, 'w', encoding='utf-8') as f:
                json.dump(new_data, f, indent=2, ensure_ascii=False)
            # Deliberately NOT applying profile['initial_prompt'] to
            # config here (see SPARK P0 fix, 2026-07-18, and the decode-
            # matrix module comment in dictation.py above
            # _SANITY_MIN_DURATION_S): a dictionary profile carrying
            # vocabulary content in initial_prompt would silently flow
            # into config['initial_prompt'] -- Priority 1 in
            # voice_training_qt.get_initial_prompt(), which every decode
            # path preserves even for free-form dictation. That's exactly
            # the destabilizing content #1 removes from every free-form
            # path elsewhere; auto-applying it here on a routine dictionary
            # load/merge would silently reopen it AND overwrite whatever
            # explicit prompt the user had set themselves. Profiles still
            # RECORD initial_prompt on save (see save_dictionary_profile)
            # as portable metadata -- they just no longer auto-apply it.
            return True, message
        except Exception as e:
            return False, f"Failed to apply profile: {e}"
    
    def delete_dictionary_profile(self, name: str) -> Tuple[bool, str]:
        """Delete a dictionary profile."""
        path = self.dictionaries_dir / f"{name}.json"
        if not path.exists():
            return False, f"Profile '{name}' not found."
        try:
            path.unlink()
            return True, f"Profile '{name}' deleted."
        except Exception as e:
            return False, f"Failed to delete profile: {e}"
    
    def export_dictionary_profile(self, name: str, export_path: str) -> Tuple[bool, str]:
        """Export a dictionary profile to an external location."""
        source = self.dictionaries_dir / f"{name}.json"
        if not source.exists():
            return False, f"Profile '{name}' not found."
        try:
            shutil.copy2(source, export_path)
            return True, f"Exported to {export_path}"
        except Exception as e:
            return False, f"Export failed: {e}"
    
    def import_dictionary_profile(self, import_path: str, 
                                  new_name: Optional[str] = None) -> Tuple[bool, str]:
        """
        Import a dictionary profile from an external file.
        
        Args:
            import_path: Path to the .json file to import
            new_name: Optional new name for the profile (uses filename if not provided)
        """
        import_path = Path(import_path)
        if not import_path.exists():
            return False, f"File not found: {import_path}"
        
        # Validate it's a valid profile
        try:
            with open(import_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # Check for required fields
            if 'vocabulary' not in data and 'corrections' not in data:
                return False, "Invalid profile: missing vocabulary or corrections."
        except Exception as e:
            return False, f"Invalid JSON file: {e}"
        
        # Determine name
        name = new_name or data.get('profile_name') or import_path.stem
        dest = self.dictionaries_dir / f"{name}.json"
        
        if dest.exists():
            return False, f"Profile '{name}' already exists. Delete it first or use a different name."
        
        try:
            shutil.copy2(import_path, dest)
            return True, f"Imported as '{name}'."
        except Exception as e:
            return False, f"Import failed: {e}"

    # -------------------------------------------------------------------------
    # One-file dictionary transfer
    # -------------------------------------------------------------------------

    def _read_json_object(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"{path.name} must contain a JSON object")
        return data

    def read_dictionary_bundle(self) -> Dict[str, Any]:
        """Return the four user-owned dictionary lists in export format."""
        training = self._read_json_object(self.training_data_path)
        wake = self._read_json_object(self.app_dir / "user_wake_corrections.json")
        aliases_file = self._read_json_object(self.app_dir / "user_aliases.json")
        aliases = aliases_file.get("aliases", aliases_file)
        if not isinstance(aliases, dict):
            raise ValueError("user_aliases.json aliases must be an object")
        vocabulary = training.get("vocabulary", [])
        corrections = training.get("corrections", {})
        if not isinstance(vocabulary, list) or not isinstance(corrections, dict):
            raise ValueError("training_data.json has invalid vocabulary or corrections")
        if not isinstance(wake, dict):
            raise ValueError("user_wake_corrections.json must contain an object")
        return {
            "format": DICTIONARY_BUNDLE_FORMAT,
            "version": DICTIONARY_BUNDLE_VERSION,
            "vocabulary": list(vocabulary),
            "corrections": dict(corrections),
            "wake_word_corrections": dict(wake),
            "personal_aliases": dict(aliases),
        }

    def export_dictionary_bundle(self, export_path: str) -> Tuple[bool, str]:
        """Export vocabulary, corrections, wake corrections, and aliases."""
        try:
            bundle = self.read_dictionary_bundle()
            destination = Path(export_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with open(destination, 'w', encoding='utf-8') as f:
                json.dump(bundle, f, indent=2, ensure_ascii=False, sort_keys=True)
            return True, f"Exported dictionary to {destination}"
        except Exception as exc:
            return False, f"Dictionary export failed: {exc}"

    @staticmethod
    def _validate_dictionary_bundle(data: Any) -> Tuple[bool, str]:
        if not isinstance(data, dict):
            return False, "Dictionary file must contain a JSON object."
        if data.get("format") != DICTIONARY_BUNDLE_FORMAT:
            return False, "Not a Samsara dictionary export."
        if data.get("version") != DICTIONARY_BUNDLE_VERSION:
            return False, f"Unsupported dictionary export version: {data.get('version')!r}."
        if not isinstance(data.get("vocabulary"), list):
            return False, "Dictionary export vocabulary must be a list."
        for key in ("corrections", "wake_word_corrections", "personal_aliases"):
            if not isinstance(data.get(key), dict):
                return False, f"Dictionary export {key} must be an object."
        return True, ""

    def _backup_dictionary_files(self) -> Optional[Path]:
        paths = (
            self.training_data_path,
            self.app_dir / "user_wake_corrections.json",
            self.app_dir / "user_aliases.json",
        )
        existing = [path for path in paths if path.exists()]
        if not existing:
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = self.app_dir / "backups" / f"dictionary-{stamp}"
        counter = 1
        while backup_dir.exists():
            backup_dir = self.app_dir / "backups" / f"dictionary-{stamp}-{counter}"
            counter += 1
        backup_dir.mkdir(parents=True, exist_ok=False)
        for path in existing:
            shutil.copy2(path, backup_dir / path.name)
        return backup_dir

    @staticmethod
    def _write_json_object(path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with open(temporary, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        try:
            os.replace(temporary, path)
        except OSError:
            # The command file watcher can hold a read handle without
            # FILE_SHARE_DELETE on Windows.  Match CommandExecutor's safe
            # fallback: overwrite through an ordinary shared-write handle.
            with open(temporary, 'r', encoding='utf-8') as f:
                text = f.read()
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
            temporary.unlink(missing_ok=True)

    def import_dictionary_bundle(self, import_path: str, mode: str = "merge") -> Tuple[bool, str]:
        """Import one dictionary bundle with explicit merge or replace semantics.

        Merge never overwrites a conflicting current key; replace takes a
        recoverable backup of each existing store before writing the bundle.
        """
        mode = str(mode or "").strip().lower()
        if mode not in {"merge", "replace"}:
            return False, "Choose either merge or replace when importing a dictionary."
        try:
            with open(import_path, 'r', encoding='utf-8') as f:
                incoming = json.load(f)
            valid, reason = self._validate_dictionary_bundle(incoming)
            if not valid:
                return False, reason
            current = self.read_dictionary_bundle()
            conflicts = 0
            if mode == "merge":
                vocabulary = list(dict.fromkeys(current["vocabulary"] + incoming["vocabulary"]))
                merged_maps = {}
                for key in ("corrections", "wake_word_corrections", "personal_aliases"):
                    merged_maps[key] = dict(current[key])
                    for name, value in incoming[key].items():
                        if name in merged_maps[key] and merged_maps[key][name] != value:
                            conflicts += 1
                        else:
                            merged_maps[key][name] = value
                imported = {
                    "vocabulary": vocabulary,
                    **merged_maps,
                }
            else:
                backup = self._backup_dictionary_files()
                imported = {
                    "vocabulary": list(incoming["vocabulary"]),
                    "corrections": dict(incoming["corrections"]),
                    "wake_word_corrections": dict(incoming["wake_word_corrections"]),
                    "personal_aliases": dict(incoming["personal_aliases"]),
                }

            self._write_json_object(
                self.training_data_path,
                {"vocabulary": imported["vocabulary"], "corrections": imported["corrections"]},
            )
            self._write_json_object(
                self.app_dir / "user_wake_corrections.json",
                imported["wake_word_corrections"],
            )
            self._write_json_object(
                self.app_dir / "user_aliases.json",
                {"version": 1, "aliases": imported["personal_aliases"]},
            )
            if mode == "replace":
                backup_text = f" Backup: {backup}." if backup else ""
                return True, f"Replaced dictionary data.{backup_text}"
            suffix = f" {conflicts} conflicting entr{'y' if conflicts == 1 else 'ies'} kept." if conflicts else ""
            return True, f"Merged dictionary data.{suffix}"
        except Exception as exc:
            return False, f"Dictionary import failed: {exc}"

    # Short names for callers that do not need to distinguish this from the
    # older per-profile import/export methods.
    export_dictionary = export_dictionary_bundle
    import_dictionary = import_dictionary_bundle

    # =========================================================================
    # Command Profile Methods
    # =========================================================================

    def _default_commands_data(self) -> Dict[str, Any]:
        source = self.default_commands_path if self.default_commands_path.exists() else self.commands_path
        data = self._read_json_object(source)
        commands = data.get("commands", data)
        if not isinstance(commands, dict):
            raise ValueError("Default command profile has no commands object")
        return {"commands": commands}

    def _backup_current_commands(self) -> Optional[Path]:
        if not self.commands_path.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.commands_path.with_name(f"{self.commands_path.name}.bak-{stamp}")
        counter = 1
        while backup.exists():
            backup = self.commands_path.with_name(f"{self.commands_path.name}.bak-{stamp}-{counter}")
            counter += 1
        shutil.copy2(self.commands_path, backup)
        return backup
    
    def list_command_profiles(self) -> List[str]:
        """Get list of available command profile names."""
        profiles = []
        if self.commands_dir.exists():
            for f in self.commands_dir.glob("*.json"):
                if f.stem != DEFAULT_COMMAND_PROFILE:
                    profiles.append(f.stem)
        return [DEFAULT_COMMAND_PROFILE] + sorted(profiles)
    
    def load_command_profile_metadata(self, name: str) -> Optional[Dict[str, Any]]:
        """Load just the metadata from a command profile."""
        if name == DEFAULT_COMMAND_PROFILE:
            try:
                return {
                    'name': DEFAULT_COMMAND_PROFILE,
                    'description': 'Built-in command set; read-only and always available.',
                    'author': 'Samsara',
                    'version': 'built-in',
                    'created': '',
                    'command_count': len(self._default_commands_data()['commands']),
                }
            except Exception:
                return None
        path = self.commands_dir / f"{name}.json"
        if not path.exists():
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return {
                'name': data.get('profile_name', name),
                'description': data.get('description', ''),
                'author': data.get('author', ''),
                'version': data.get('version', '1.0'),
                'created': data.get('created', ''),
                'command_count': len(data.get('commands', {}))
            }
        except Exception:
            return None
    
    def save_command_profile(self, name: str, description: str = "",
                            author: str = "", overwrite: bool = False) -> Tuple[bool, str]:
        """Save current commands as a named profile."""
        if name == DEFAULT_COMMAND_PROFILE:
            return False, "The built-in Default profile is read-only."
        path = self.commands_dir / f"{name}.json"
        
        if path.exists() and not overwrite:
            return False, f"Profile '{name}' already exists."
        
        try:
            with open(self.commands_path, 'r', encoding='utf-8') as f:
                current_data = json.load(f)
        except Exception as e:
            return False, f"Could not read current commands: {e}"
        
        profile = {
            'profile_name': name,
            'description': description,
            'author': author,
            'version': '1.0',
            'created': datetime.now().strftime('%Y-%m-%d'),
            'commands': current_data.get('commands', {})
        }
        
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(profile, f, indent=2, ensure_ascii=False)
            return True, f"Command profile '{name}' saved."
        except Exception as e:
            return False, f"Failed to save: {e}"
    
    def load_command_profile(self, name: str, merge: bool = False) -> Tuple[bool, str]:
        """Load a command profile, either replacing or merging."""
        if name == DEFAULT_COMMAND_PROFILE:
            if merge:
                return False, "The built-in Default profile can only replace the current command set."
            try:
                backup = self._backup_current_commands()
                self._write_json_object(self.commands_path, self._default_commands_data())
                detail = f" Backup: {backup}." if backup else ""
                return True, f"Restored the built-in Default command profile.{detail}"
            except Exception as exc:
                return False, f"Could not restore Default: {exc}"
        path = self.commands_dir / f"{name}.json"
        
        if not path.exists():
            return False, f"Profile '{name}' not found."
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                profile = json.load(f)
        except Exception as e:
            return False, f"Could not read profile: {e}"
        
        if merge:
            try:
                with open(self.commands_path, 'r', encoding='utf-8') as f:
                    current = json.load(f)
            except Exception:
                current = {'commands': {}}
            
            # Merge (profile commands override existing ones with same name)
            merged = current.get('commands', {}).copy()
            new_commands = profile.get('commands', {})
            added = sum(1 for k in new_commands if k not in merged)
            merged.update(new_commands)
            
            new_data = {'commands': merged}
            message = f"Merged '{name}': added {added} new commands."
        else:
            new_data = {'commands': profile.get('commands', {})}
            message = f"Loaded profile '{name}' (replaced current commands)."
        
        try:
            with open(self.commands_path, 'w', encoding='utf-8') as f:
                json.dump(new_data, f, indent=2, ensure_ascii=False)
            return True, message
        except Exception as e:
            return False, f"Failed to apply: {e}"
    
    def delete_command_profile(self, name: str) -> Tuple[bool, str]:
        """Delete a command profile."""
        if name == DEFAULT_COMMAND_PROFILE:
            return False, "The built-in Default profile is read-only."
        path = self.commands_dir / f"{name}.json"
        if not path.exists():
            return False, f"Profile '{name}' not found."
        try:
            path.unlink()
            return True, f"Profile '{name}' deleted."
        except Exception as e:
            return False, f"Failed to delete: {e}"
    
    def export_command_profile(self, name: str, export_path: str) -> Tuple[bool, str]:
        """Export a command profile to an external location."""
        if name == DEFAULT_COMMAND_PROFILE:
            try:
                with open(export_path, 'w', encoding='utf-8') as f:
                    json.dump({
                        'profile_name': DEFAULT_COMMAND_PROFILE,
                        'description': 'Built-in command set; read-only and always available.',
                        **self._default_commands_data(),
                    }, f, indent=2, ensure_ascii=False)
                return True, f"Exported to {export_path}"
            except Exception as exc:
                return False, f"Export failed: {exc}"
        source = self.commands_dir / f"{name}.json"
        if not source.exists():
            return False, f"Profile '{name}' not found."
        try:
            shutil.copy2(source, export_path)
            return True, f"Exported to {export_path}"
        except Exception as e:
            return False, f"Export failed: {e}"
    
    def import_command_profile(self, import_path: str,
                               new_name: Optional[str] = None) -> Tuple[bool, str]:
        """Import a command profile from an external file."""
        import_path = Path(import_path)
        if not import_path.exists():
            return False, f"File not found: {import_path}"
        
        try:
            with open(import_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if 'commands' not in data:
                return False, "Invalid profile: missing 'commands' field."
        except Exception as e:
            return False, f"Invalid JSON: {e}"
        
        name = new_name or data.get('profile_name') or import_path.stem
        if name == DEFAULT_COMMAND_PROFILE:
            return False, "Default is built-in and read-only; choose another profile name."
        dest = self.commands_dir / f"{name}.json"
        
        if dest.exists():
            return False, f"Profile '{name}' already exists."
        
        try:
            shutil.copy2(import_path, dest)
            return True, f"Imported as '{name}'."
        except Exception as e:
            return False, f"Import failed: {e}"
    
    # =========================================================================
    # Helper Methods
    # =========================================================================

    def get_active_profile_names(self) -> Dict[str, Optional[str]]:
        """
        Get the names of currently active profiles from config.
        Returns dict with 'dictionary' and 'commands' keys.
        """
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            return {
                'dictionary': config.get('active_dictionary_profile'),
                'commands': config.get('active_command_profile')
            }
        except Exception:
            return {'dictionary': None, 'commands': None}
    
    def set_active_profile_names(self, dictionary: Optional[str] = None,
                                 commands: Optional[str] = None):
        """Update the active profile names in config."""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            if dictionary is not None:
                config['active_dictionary_profile'] = dictionary
            if commands is not None:
                config['active_command_profile'] = commands
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"set_active_profile_names: {e}")
