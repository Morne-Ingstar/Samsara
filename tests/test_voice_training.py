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
    """Create a VoiceTrainingWindow with mocked dependencies"""
    # Create training data file if needed
    training_data = {
        'vocabulary': custom_vocab or [],
        'corrections': corrections or {}
    }
    training_file = tmp_path / 'training_data.json'
    with open(training_file, 'w') as f:
        json.dump(training_data, f)

    # Mock the app
    mock_app = Mock()
    mock_app.config_path = tmp_path / 'config.json'
    mock_app.config = {'initial_prompt': ''}

    # Patch UI components
    with patch('voice_training.ctk'):
        with patch('voice_training.sd'):
            from voice_training import VoiceTrainingWindow
            vt = VoiceTrainingWindow(mock_app)
            return vt


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

    def test_apply_corrections_case_sensitive(self, tmp_path):
        """Test that corrections are case-sensitive"""
        corrections = {'Hello': 'Hi'}
        vt = create_test_voice_training(tmp_path, corrections=corrections)

        result = vt.apply_corrections("Hello world")
        assert result == "Hi world"

        result = vt.apply_corrections("hello world")
        assert result == "hello world"  # lowercase not matched

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
        # Create app mock pointing to non-existent training file
        mock_app = Mock()
        mock_app.config_path = tmp_path / 'config.json'
        mock_app.config = {'initial_prompt': ''}

        with patch('voice_training.ctk'):
            with patch('voice_training.sd'):
                from voice_training import VoiceTrainingWindow
                vt = VoiceTrainingWindow(mock_app)

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

        with patch('voice_training.ctk'):
            with patch('voice_training.sd'):
                from voice_training import VoiceTrainingWindow
                vt = VoiceTrainingWindow(mock_app)

                # Should fall back to empty defaults
                assert vt.custom_vocab == []
                assert vt.corrections_dict == {}
