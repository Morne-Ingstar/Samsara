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


class ProfileManager:
    """Manages dictionary and command profiles for Samsara."""
    
    def __init__(self, app_dir: str):
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
        self.commands_path = self.app_dir / "commands.json"
        self.config_path = self.app_dir / "config.json"
        
        # Ensure directories exist
        self._ensure_directories()
    
    def _ensure_directories(self):
        """Create profile directories if they don't exist."""
        self.dictionaries_dir.mkdir(parents=True, exist_ok=True)
        self.commands_dir.mkdir(parents=True, exist_ok=True)
    
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
        except Exception:
            pass
        
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
            
            # Also update initial_prompt in config if present in profile
            if profile.get('initial_prompt'):
                self._update_config_initial_prompt(profile['initial_prompt'])
            
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
    
    # =========================================================================
    # Command Profile Methods
    # =========================================================================
    
    def list_command_profiles(self) -> List[str]:
        """Get list of available command profile names."""
        profiles = []
        if self.commands_dir.exists():
            for f in self.commands_dir.glob("*.json"):
                profiles.append(f.stem)
        return sorted(profiles)
    
    def load_command_profile_metadata(self, name: str) -> Optional[Dict[str, Any]]:
        """Load just the metadata from a command profile."""
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
    
    def _update_config_initial_prompt(self, prompt: str):
        """Update the initial_prompt in config.json."""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            config['initial_prompt'] = prompt
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except Exception:
            pass  # Non-critical, ignore errors
    
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
        except Exception:
            pass
