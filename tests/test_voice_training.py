"""
Tests for VoiceTrainingWindow class.
Tests corrections, vocabulary, and initial prompt building.
"""
import pytest
import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================================
# Helper to create VoiceTrainingWindow for testing
# ============================================================================

def create_test_voice_training(tmp_path, custom_vocab=None, corrections=None):
    """Create a VoiceTrainingQt instance with test data pre-loaded."""
    training_data = {
        'vocabulary': custom_vocab or [],
        'corrections': corrections or {}
    }
    training_file = tmp_path / 'training_data.json'
    with open(training_file, 'w') as f:
        json.dump(training_data, f)

    mock_app = Mock()
    mock_app.config_path = tmp_path / 'config.json'
    mock_app.config = {'initial_prompt': ''}

    from samsara.ui.voice_training_qt import VoiceTrainingQt
    return VoiceTrainingQt(mock_app)


# ============================================================================
# Corrections Tests
# ============================================================================

class TestCorrections:
    """Tests for the corrections dictionary"""

    def test_apply_simple_correction(self, tmp_path):
        """Test applying a simple word correction"""
        corrections = {'teh': 'the', 'recieve': 'receive'}
        vt = create_test_voice_training(tmp_path, corrections=corrections)

        result = vt.apply_corrections("I recieve teh package")
        assert result == "I receive the package"

    def test_apply_multiple_corrections(self, tmp_path):
        """Test applying multiple corrections to same text"""
        corrections = {'foo': 'bar', 'baz': 'qux'}
        vt = create_test_voice_training(tmp_path, corrections=corrections)

        result = vt.apply_corrections("foo and baz")
        assert result == "bar and qux"

    def test_apply_no_corrections_needed(self, tmp_path):
        """Test text that needs no corrections"""
        corrections = {'teh': 'the'}
        vt = create_test_voice_training(tmp_path, corrections=corrections)

        result = vt.apply_corrections("the quick brown fox")
        assert result == "the quick brown fox"

    def test_apply_corrections_empty_dict(self, tmp_path):
        """Test with empty corrections dictionary"""
        vt = create_test_voice_training(tmp_path, corrections={})

        result = vt.apply_corrections("hello world")
        assert result == "hello world"

    def test_apply_corrections_case_insensitive(self, tmp_path):
        """Corrections match regardless of the case Whisper returned."""
        corrections = {'hello': 'hi'}
        vt = create_test_voice_training(tmp_path, corrections=corrections)

        result = vt.apply_corrections("Hello world")
        assert result == "Hi world"

        result = vt.apply_corrections("HELLO world")
        assert result == "HI world"

    def test_apply_corrections_preserves_case(self, tmp_path):
        """Replacement case is derived from the matched text, not the stored value."""
        corrections = {'hello': 'hi'}
        vt = create_test_voice_training(tmp_path, corrections=corrections)

        assert vt.apply_corrections("hello world") == "hi world"
        assert vt.apply_corrections("Hello world") == "Hi world"
        assert vt.apply_corrections("HELLO world") == "HI world"

    def test_apply_corrections_word_boundary(self, tmp_path):
        """Corrections must not match inside a larger word."""
        corrections = {'cat': 'Kat'}
        vt = create_test_voice_training(tmp_path, corrections=corrections)

        result = vt.apply_corrections("catalog")
        assert result == "catalog"

    def test_apply_corrections_no_chaining(self, tmp_path):
        """A replacement's output must never be re-matched by another rule."""
        corrections = {'foo': 'bar', 'bar': 'baz'}
        vt = create_test_voice_training(tmp_path, corrections=corrections)

        result = vt.apply_corrections("foo")
        assert result == "bar"

    def test_apply_corrections_longest_first(self, tmp_path):
        """A longer overlapping key must win over a shorter prefix key."""
        corrections = {'going': 'X', 'going to': 'gonna'}
        vt = create_test_voice_training(tmp_path, corrections=corrections)

        result = vt.apply_corrections("I'm going to leave")
        assert result == "I'm gonna leave"

    def test_apply_corrections_phrase(self, tmp_path):
        """Test correcting phrases, not just words"""
        corrections = {'kind of': 'kinda', 'going to': 'gonna'}
        vt = create_test_voice_training(tmp_path, corrections=corrections)

        result = vt.apply_corrections("I'm going to be kind of late")
        assert result == "I'm gonna be kinda late"


# ============================================================================
# Vocabulary Tests
# ============================================================================

class TestVocabulary:
    """Tests for custom vocabulary"""

    def test_load_vocabulary(self, tmp_path):
        """Test loading vocabulary from file"""
        vocab = ['TensorFlow', 'PyTorch', 'NumPy']
        vt = create_test_voice_training(tmp_path, custom_vocab=vocab)

        assert vt.custom_vocab == vocab

    def test_load_empty_vocabulary(self, tmp_path):
        """Test loading with no vocabulary"""
        vt = create_test_voice_training(tmp_path, custom_vocab=[])

        assert vt.custom_vocab == []

    def test_save_vocabulary(self, tmp_path):
        """Test saving vocabulary to file"""
        vt = create_test_voice_training(tmp_path)
        vt.custom_vocab = ['word1', 'word2', 'word3']

        result = vt.save_training_data()

        assert result is True
        training_file = tmp_path / 'training_data.json'
        with open(training_file) as f:
            data = json.load(f)
        assert data['vocabulary'] == ['word1', 'word2', 'word3']


# ============================================================================
# Initial Prompt Tests
# ============================================================================

class TestInitialPrompt:
    """Tests for initial prompt building"""

    def test_get_prompt_with_vocabulary(self, tmp_path):
        """Test building prompt with vocabulary"""
        vocab = ['API', 'JSON', 'HTTP']
        vt = create_test_voice_training(tmp_path, custom_vocab=vocab)

        result = vt.get_initial_prompt()

        assert result is not None
        assert 'API' in result
        assert 'JSON' in result
        assert 'HTTP' in result
        assert 'Common terms' in result

    def test_get_prompt_with_custom_prompt(self, tmp_path):
        """Test building prompt with custom prompt"""
        vt = create_test_voice_training(tmp_path)
        vt.app.config['initial_prompt'] = 'Technical programming discussion'

        result = vt.get_initial_prompt()

        assert result is not None
        assert 'Technical programming discussion' in result

    def test_get_prompt_combined(self, tmp_path):
        """Test building prompt with both vocabulary and custom prompt"""
        vocab = ['Python', 'JavaScript']
        vt = create_test_voice_training(tmp_path, custom_vocab=vocab)
        vt.app.config['initial_prompt'] = 'Code review'

        result = vt.get_initial_prompt()

        assert result is not None
        assert 'Code review' in result
        assert 'Python' in result
        assert 'JavaScript' in result

    def test_get_prompt_empty(self, tmp_path):
        """Test getting prompt with no vocabulary or custom prompt"""
        vt = create_test_voice_training(tmp_path, custom_vocab=[])
        vt.app.config['initial_prompt'] = ''

        result = vt.get_initial_prompt()

        assert result is None

    def test_get_prompt_respects_char_budget(self, tmp_path):
        """With an oversized command vocabulary, the prompt must stay within
        the ~800-char (~224 token) Whisper budget. Custom prompt and custom
        vocabulary rank higher priority than command vocabulary, so they must
        survive in full while command vocabulary is truncated item-by-item."""
        vt = create_test_voice_training(tmp_path, custom_vocab=['AlphaTerm', 'BetaTerm'])
        vt.app.command_executor = None
        vt.app.config['initial_prompt'] = 'Custom context for this dictation session.'
        vt.app.config['web_shortcuts'] = {
            f'oversized vocabulary phrase number {i:03d}': 'https://example.com'
            for i in range(60)
        }

        result = vt.get_initial_prompt()

        assert result is not None
        assert len(result) <= 800
        assert result.startswith('Custom context for this dictation session.')
        assert 'Common terms: AlphaTerm, BetaTerm' in result

    def test_include_vocabulary_false_omits_command_and_common_terms(self, tmp_path):
        """SPARK P0 fix (2026-07-18, decode matrix N=10/cell against both
        incident WAVs): free-form dictation paths must not receive EITHER
        the auto-derived command-phrase vocabulary (Priority 3) OR the
        trained "Common terms" vocabulary (Priority 2) -- the matrix proved
        the short 9-term Common-terms list alone reproduces the identical
        destabilization signature as the full command list. Only Priority 1
        (explicit config['initial_prompt']) survives include_vocabulary=False.

        Supersedes the narrower 2026-07-16 contract (commit 02e00b9), which
        gated only Priority 3 under the name include_commands -- that
        parameter is renamed to include_vocabulary and now gates Priority 2
        as well; see samsara/ui/voice_training_qt.get_initial_prompt."""
        vt = create_test_voice_training(tmp_path, custom_vocab=['AlphaTerm', 'BetaTerm'])
        vt.app.command_executor = None
        vt.app.config['initial_prompt'] = 'Custom context for this dictation session.'
        vt.app.config['web_shortcuts'] = {
            f'oversized vocabulary phrase number {i:03d}': 'https://example.com'
            for i in range(60)
        }

        with_vocabulary = vt.get_initial_prompt(include_vocabulary=True)
        without_vocabulary = vt.get_initial_prompt(include_vocabulary=False)

        assert 'Voice commands:' in with_vocabulary
        assert 'Common terms: AlphaTerm, BetaTerm' in with_vocabulary
        assert 'Voice commands:' not in without_vocabulary
        assert 'Common terms:' not in without_vocabulary
        # Priority 1 (explicit, user-set override) survives regardless --
        # it's not auto-derived vocabulary.
        assert without_vocabulary == 'Custom context for this dictation session.'

    def test_include_vocabulary_false_with_no_explicit_prompt_is_empty(self, tmp_path):
        """The common case: no explicit config['initial_prompt'] override
        set. A free-form path must get a genuinely empty/None prompt, not
        just "vocabulary-free but still something" -- this is what "empty
        for free-form dictation" cashes out to when Priority 1 is unset."""
        vt = create_test_voice_training(tmp_path, custom_vocab=['AlphaTerm', 'BetaTerm'])
        vt.app.command_executor = None
        vt.app.config['initial_prompt'] = ''

        result = vt.get_initial_prompt(include_vocabulary=False)
        assert not result  # '' or None, both mean "no prompt sent to Whisper"

    def test_include_vocabulary_defaults_to_true(self, tmp_path):
        """Every OTHER decode path (command-lane utterances) calls
        get_initial_prompt() with no argument and must keep getting the full
        vocabulary -- only free-form paths opt out explicitly."""
        vt = create_test_voice_training(tmp_path)
        vt.app.command_executor = None
        vt.app.config['web_shortcuts'] = {'oversized vocabulary phrase': 'https://example.com'}

        assert vt.get_initial_prompt() == vt.get_initial_prompt(include_vocabulary=True)


# ============================================================================
# Similarity Calculation Tests
# ============================================================================

class TestSimilarityCalculation:
    """Tests for string similarity calculation"""

    def test_similarity_identical_strings(self, tmp_path):
        """Test similarity of identical strings"""
        vt = create_test_voice_training(tmp_path)

        result = vt.calculate_similarity("hello world", "hello world")
        assert result == 100.0

    def test_similarity_completely_different(self, tmp_path):
        """Test similarity of completely different strings"""
        vt = create_test_voice_training(tmp_path)

        result = vt.calculate_similarity("hello world", "foo bar")
        assert result == 0.0

    def test_similarity_partial_match(self, tmp_path):
        """Test similarity with partial word match"""
        vt = create_test_voice_training(tmp_path)

        result = vt.calculate_similarity("hello world", "hello there")
        # 1 word in common (hello), 3 total unique words
        assert result == pytest.approx(33.33, rel=0.1)

    def test_similarity_empty_strings(self, tmp_path):
        """Test similarity of empty strings"""
        vt = create_test_voice_training(tmp_path)

        result = vt.calculate_similarity("", "")
        assert result == 100.0

    def test_similarity_one_empty(self, tmp_path):
        """Test similarity when one string is empty"""
        vt = create_test_voice_training(tmp_path)

        result = vt.calculate_similarity("hello world", "")
        assert result == 0.0

    def test_similarity_case_sensitive(self, tmp_path):
        """Test that similarity is case-sensitive"""
        vt = create_test_voice_training(tmp_path)

        result = vt.calculate_similarity("Hello World", "hello world")
        # Different case = different words
        assert result == 0.0


# ============================================================================
# Training Data Persistence Tests
# ============================================================================

class TestTrainingDataPersistence:
    """Tests for training data loading and saving"""

    def test_load_training_data(self, tmp_path):
        """Test loading training data from file"""
        # Use the helper with the data we want to test
        vt = create_test_voice_training(
            tmp_path,
            custom_vocab=['word1', 'word2'],
            corrections={'wrong': 'right'}
        )

        assert vt.custom_vocab == ['word1', 'word2']
        assert vt.corrections_dict == {'wrong': 'right'}

    def test_load_training_data_missing_file(self, tmp_path):
        """Test loading when training file doesn't exist"""
        mock_app = Mock()
        mock_app.config_path = tmp_path / 'config.json'
        mock_app.config = {'initial_prompt': ''}

        from samsara.ui.voice_training_qt import VoiceTrainingQt
        vt = VoiceTrainingQt(mock_app)

        assert vt.custom_vocab == []
        assert vt.corrections_dict == {}

    def test_save_training_data(self, tmp_path):
        """Test saving training data to file"""
        vt = create_test_voice_training(tmp_path)
        vt.custom_vocab = ['new_word']
        vt.corrections_dict = {'typo': 'fixed'}

        result = vt.save_training_data()

        assert result is True
        training_file = tmp_path / 'training_data.json'
        with open(training_file) as f:
            data = json.load(f)
        assert data['vocabulary'] == ['new_word']
        assert data['corrections'] == {'typo': 'fixed'}

    def test_load_training_data_invalid_json(self, tmp_path):
        """Test loading corrupted training file"""
        training_file = tmp_path / 'training_data.json'
        training_file.write_text('invalid json {{{')

        mock_app = Mock()
        mock_app.config_path = tmp_path / 'config.json'
        mock_app.config = {'initial_prompt': ''}

        from samsara.ui.voice_training_qt import VoiceTrainingQt
        vt = VoiceTrainingQt(mock_app)

        # Should fall back to empty defaults
        assert vt.custom_vocab == []
        assert vt.corrections_dict == {}
