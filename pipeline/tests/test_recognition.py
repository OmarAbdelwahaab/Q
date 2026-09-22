from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from pipeline.recognition.corpus import CanonicalVerse, QuranCorpus
from pipeline.recognition.asr import TranscriptionError
from pipeline.recognition.matcher import QuranMatcher, normalize_arabic
from pipeline.recognition.service import RecognitionService
from pipeline.state.repository import PipelineStateRepository


class StubTranscriber:
    def __init__(self, text: str | Exception) -> None:
        self.text = text

    def transcribe(self, _: Path) -> str:
        if isinstance(self.text, Exception):
            raise self.text
        return self.text


class RecordingAlerts:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_recognition_failure(self, message: str) -> None:
        self.messages.append(message)


class RecognitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = QuranCorpus([
            CanonicalVerse(1, 1, "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ"),
            CanonicalVerse(1, 2, "الْحَمْدُ لِلَّهِ رَبِّ الْعَالَمِينَ"),
            CanonicalVerse(1, 3, "الرَّحْمَٰنِ الرَّحِيمِ"),
            CanonicalVerse(2, 1, "الم"),
        ])

    def test_normalization_and_contiguous_range_match(self) -> None:
        match = QuranMatcher(self.corpus).match("بسم الله الرحمن الرحيم الحمد لله رب العالمين")
        self.assertEqual((match.surah, match.ayah_start, match.ayah_end), (1, 1, 2))
        self.assertGreaterEqual(match.confidence, 0.95)
        self.assertEqual(normalize_arabic("الرَّحْمَٰنِ"), "الرحمن")

    def test_completed_recognition_writes_spec_match_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "audio" / "401.wav"
            audio.parent.mkdir(); audio.write_bytes(b"fixture")
            state, alerts = PipelineStateRepository(root / "state.db"), RecordingAlerts()
            service = RecognitionService(root, state, alerts, StubTranscriber("بسم الله الرحمن الرحيم"), QuranMatcher(self.corpus))
            result = asyncio.run(service.recognize(401))
            self.assertEqual(result.status, "completed")
            artifact = json.loads((root / "match" / "401.json").read_text(encoding="utf-8"))
            self.assertEqual({"surah", "ayah_start", "ayah_end", "canonical_text", "match_confidence", "recognized_text"}, set(artifact))
            self.assertEqual(artifact["surah"], 1)
            self.assertEqual(state.fetch_stage(401, "recognition")["status"], "completed")
            self.assertEqual(alerts.messages, [])

    def test_asr_failure_marks_state_and_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, alerts = PipelineStateRepository(root / "state.db"), RecordingAlerts()
            service = RecognitionService(root, state, alerts, StubTranscriber(TranscriptionError("endpoint unavailable")), QuranMatcher(self.corpus))
            result = asyncio.run(service.recognize(402))
            self.assertEqual(result.status, "failed")
            self.assertIn("endpoint unavailable", result.error or "")
            self.assertEqual(state.fetch_stage(402, "recognition")["status"], "failed")
            self.assertEqual(len(alerts.messages), 1)

    def test_cached_corpus_is_used_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "quran.json"
            cache.write_text(json.dumps([{"surah": 1, "ayah": 1, "text": "بِسْمِ اللَّهِ"}], ensure_ascii=False), encoding="utf-8")
            corpus = QuranCorpus.load_or_fetch(cache, "https://invalid.example")
            self.assertEqual(corpus.verses[0].text, "بِسْمِ اللَّهِ")

    def test_bundled_corpus_is_used_when_cache_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cold_boot_cache.json"
            self.assertFalse(cache.exists())
            corpus = QuranCorpus.load_or_fetch(cache, "https://invalid.example-must-not-call")
            self.assertEqual(len(corpus.verses), 6236)
            self.assertTrue(cache.exists())
            self.assertEqual(corpus.verses[0].surah, 1)
            self.assertEqual(corpus.verses[0].ayah, 1)

    def test_whisper_transcriber_requires_api_key_for_cloud(self) -> None:
        from pipeline.recognition.asr import WhisperTranscriber

        transcriber = WhisperTranscriber("https://api.groq.com/openai/v1/audio/transcriptions", None, "whisper-large-v3")
        with tempfile.TemporaryDirectory() as directory:
            fake_audio = Path(directory) / "audio.wav"
            fake_audio.write_bytes(b"dummy audio")
            with self.assertRaises(TranscriptionError) as ctx:
                transcriber.transcribe(fake_audio)
            self.assertIn("ASR_API_KEY is not configured", str(ctx.exception))

    def test_whisper_transcriber_formats_http_error_with_body(self) -> None:
        import io
        import urllib.error
        from unittest.mock import patch
        from pipeline.recognition.asr import WhisperTranscriber

        transcriber = WhisperTranscriber("https://api.groq.com/openai/v1/audio/transcriptions", "gsk_test123", "whisper-large-v3")
        with tempfile.TemporaryDirectory() as directory:
            fake_audio = Path(directory) / "audio.wav"
            fake_audio.write_bytes(b"dummy audio")

            err_body = io.BytesIO(b'{"error": {"message": "Invalid API Key provided"}}')
            http_err = urllib.error.HTTPError(
                url="https://api.groq.com/openai/v1/audio/transcriptions",
                code=401,
                msg="Unauthorized",
                hdrs={},
                fp=err_body,
            )

            with patch("urllib.request.urlopen", side_effect=http_err):
                with self.assertRaises(TranscriptionError) as ctx:
                    transcriber.transcribe(fake_audio)
                self.assertIn("401", str(ctx.exception))
                self.assertIn("Invalid API Key provided", str(ctx.exception))
                self.assertIn("Verify that ASR_API_KEY is correctly set", str(ctx.exception))

    def test_normalize_arabic_preserves_alef_wasla(self) -> None:
        # \u0671 is alef wasla (ٱ)
        self.assertEqual(normalize_arabic("ٱلْحَمْدُ"), "الحمد")
        self.assertEqual(normalize_arabic("ٱلَّذِينَ"), "الذين")
        self.assertEqual(normalize_arabic("ٱسْتَعِينُوا"), "استعينوا")
        self.assertEqual(normalize_arabic("ٱلسَّمَـٰوَٰتِ"), "السموت")

    def test_multi_surah_prayer_sequence_matching(self) -> None:
        corpus = QuranCorpus([
            CanonicalVerse(1, 1, "بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ"),
            CanonicalVerse(1, 2, "ٱلْحَمْدُ لِلَّهِ رَبِّ ٱلْعَـٰلَمِينَ"),
            CanonicalVerse(105, 1, "أَلَمْ تَرَ كَيْفَ فَعَلَ رَبُّكَ بِأَصْحَـٰبِ ٱلْفِيلِ"),
            CanonicalVerse(105, 2, "أَلَمْ يَجْعَلْ كَيْدَهُمْ فِى تَضْلِيلٍ"),
        ])
        transcript = "الحمد لله رب العالمين آمين ألم تر كيف فعل ربك بأصحاب الفيل ألم يجعل كيدهم في تضليل"
        match = QuranMatcher(corpus).match(transcript)
        # Should identify Surah 105 as primary non-Fatiha surah
        self.assertEqual(match.surah, 105)
        self.assertEqual(match.ayah_start, 1)
        self.assertEqual(match.ayah_end, 2)
        self.assertGreaterEqual(match.confidence, 0.90)
        # Canonical text must contain both Al-Fatiha and Al-Fil in order
        self.assertIn("ٱلْحَمْدُ لِلَّهِ", match.canonical_text)
        self.assertIn("بِأَصْحَـٰبِ ٱلْفِيلِ", match.canonical_text)

    def test_repeated_ayah_matching(self) -> None:
        corpus = QuranCorpus([
            CanonicalVerse(106, 3, "فَلْيَعْبُدُوا۟ رَبَّ هَـٰذَا ٱلْبَيْتِ"),
            CanonicalVerse(106, 4, "ٱلَّذِىٓ أَطْعَمَهُم مِّن جُوعٍ وَءَامَنَهُم مِّنْ خَوْفٍۭ"),
        ])
        # Reciter repeats 106:3 twice
        transcript = "فليعبدوا رب هذا البيت فليعبدوا رب هذا البيت الذي أطعمهم من جوع وآمنهم من خوف"
        match = QuranMatcher(corpus).match(transcript)
        self.assertGreaterEqual(match.confidence, 0.90)
        # Repeated ayah must appear twice in canonical_text
        self.assertEqual(match.canonical_text.count("فَلْيَعْبُدُوا۟"), 2)

    def test_partial_ayah_phrase_repetition(self) -> None:
        corpus = QuranCorpus([
            CanonicalVerse(5, 118, "إِن تُعَذِّبْهُمْ فَإِنَّهُمْ عِبَادُكَ ۖ وَإِن تَغْفِرْ لَهُمْ فَإِنَّكَ أَنتَ ٱلْعَزِيزُ ٱلْحَكِيمُ"),
        ])
        # Reciter recites full verse, then repeats the second phrase
        transcript = "إن تعذبهم فإنهم عبادك وإن تغفر لهم فإنك أنت العزيز الحكيم وإن تغفر لهم فإنك أنت العزيز الحكيم"
        match = QuranMatcher(corpus).match(transcript)
        self.assertEqual(match.surah, 5)
        self.assertEqual(match.ayah_start, 118)
        self.assertEqual(match.ayah_end, 118)
        self.assertGreaterEqual(match.confidence, 0.90)
        self.assertEqual(match.canonical_text.count("وَإِن تَغْفِرْ لَهُمْ"), 2)

    def test_skipped_verses_matching(self) -> None:
        corpus = QuranCorpus([
            CanonicalVerse(23, 84, "قُل لِّمَنِ ٱلْأَرْضُ وَمَن فِيهَآ إِن كُنتُمْ تَعْلَمُونَ"),
            CanonicalVerse(23, 85, "سَيَقُولُونَ لِلَّهِ ۚ قُلْ أَفَلَا تَذَكَّرُونَ"),
            CanonicalVerse(23, 86, "قُلْ مَن رَّبُّ ٱلسَّمَـٰوَٰتِ ٱلسَّبْعِ وَرَبُّ ٱلْعَرْشِ ٱلْعَظِيمِ"),
            CanonicalVerse(23, 89, "سَيَقُولُونَ لِلَّهِ ۚ قُلْ فَأَنَّىٰ تُسْحَرُونَ"),
        ])
        # Recites 84, 85, then skips to 89
        transcript = "قل لمن الأرض ومن فيها إن كنتم تعلمون سيقولون لله قل أفلا تذكرون سيقولون لله قل فأنا تسحرون"
        match = QuranMatcher(corpus).match(transcript)
        self.assertEqual(match.surah, 23)
        self.assertEqual(match.ayah_start, 84)
        self.assertEqual(match.ayah_end, 89)
        self.assertGreaterEqual(match.confidence, 0.90)
        self.assertIn("قُل لِّمَنِ ٱلْأَرْضُ", match.canonical_text)
        self.assertIn("قُلْ فَأَنَّىٰ تُسْحَرُونَ", match.canonical_text)
        # 86 was not recited, so it must not be in canonical text
        self.assertNotIn("ٱلسَّمَـٰوَٰتِ ٱلسَّبْعِ", match.canonical_text)

    def test_live_message_13506_and_13509_transcripts(self) -> None:
        bundled = Path(__file__).resolve().parent.parent / "assets" / "corpus" / "quran-uthmani.json"
        if not bundled.is_file():
            self.skipTest("Bundled corpus not present")
        corpus = QuranCorpus._from_json(bundled.read_text(encoding="utf-8"))
        matcher = QuranMatcher(corpus)

        # Message 13509 (2-rak'ah prayer with repeated 106:3)
        t13509 = "الحمد لله رب العالمين الرحمن الرحيم مالك يوم الدين إياك نعبد وإياك نستعين إهدنا الصراط المستقيم صراط الذين أنعمت عليهم غير المغضوب عليهم ولا الضالين آمين ألم تر كيف فعل ربك بأصحاب الفيل ألم يجعل كيدهم في تضليل وأرسل عليهم طيرا أبابيل ترميهم بحجارة من سجيل مالك يوم الدين إياك نعبد وإياك نستعين إهدنا الصراط المستقيم صراط الذين أنعمت عليهم غير المغضوب عليهم ولا الضالين آمين لإلاف قريش إلافهم رحلة الشتاء والصيف فليعبدوا رب هذا البيت فليعبدوا رب هذا البيت الذي أطعمهم من جوع وآمنهم من خوف"
        m13509 = matcher.match(t13509)
        self.assertGreaterEqual(m13509.confidence, 0.90)
        self.assertEqual(m13509.canonical_text.count("فَلْيَعْبُدُوا۟"), 2)

        # Message 13506 (41 verses with skips)
        t13506 = "الحمد لله رب العالمين الرحمن الرحيم مالك يوم الدين إياك نعبد وإياك نستعين اهدنا الصراط المستقيم صراط الذين أنعمت عليهم غير المغضوب عليهم ولا الظالين آمين وهو الذي أنشأ لكم السمع والأبصار والأفئدة قليلا ما تشكرون وهو الذي ذرأكم في الأرض وإليه تحشرون وهو الذي يحيي ويميت وله اختلاف الليل والنهار أفلا تعقلون بل قالوا مثل ما قال الأولون قالوا أئذا متنا وكنا ترابا وعظاما أئنا لمبعوثون لقد وعدنا نحن وآباؤنا هذا من قبل إن هذا إلا أساطير الأولين قل لمن الأرض ومن فيها إن كنتم تعلمون سيقولون لله قل أفلا تذكرون سيقولون لله قل فأنا تسحرون بل أتيناهم بالحق وإنهم لكاذبون ما اتخذ الله من ولد وما كان معه من إله إذا لذهب كل إله بما خلق ولعلى بعضهم على بعض سبحان الله عما يصفون عالم الغيب والشهادة فتعالى عما يشركون قل رب إما تريني ما يوعدون رب فلا تجعلني في القوم الظالمين وإنا على أن نريك ما نعدهم لقادرون ادفع بالتي هي أحسن السيئة نحن أعلم بما يصيفون وقل رب أعوذ بك من همزات الشياطين وأعوذ بك رب أن يحضرون حتى إذا جاء أحدهم الموت قال رب ارجعون لعلي أعمل صالحا فيما تركت فإذا نفخ في الصور فلا أنساب بينهم يومئذ ولا يتساءلون فمن ثقلت موازينه فأولئك هم المفلحون ومن خفت موازينه فأولئك الذين خسروا أنفسهم في جهنم خالدون تلفح وجوههم النار وهم فيها كالحون ألم تكن آياتي تتلى عليكم فكنتن بها تكذبون قالوا ربنا غلبت علينا شقوتنا وكنا قوما ضالين ربنا أخرجنا منها فإن عدنا فإنا ظالمون قال اخسأوا فيها ولا تكلمون إني جزيتهم اليوم بما صبروا أنهم هم الفائزون قال كم لبثتم في الأرض عدد سنين قالوا لبثنا يوما أو بعض يوم فاسأل العادين قال إن لبثتم إلا قليلا لو أنكم كنتم تعلمون أفحسبتم أنما خلقناكم عبثا وأنكم إلينا لا ترجعون فتعالى الله الملك الحق لا إله إلا هو رب العرش الكريم ومن يدعو مع الله إلها آخر لا برهان له به فإنما حسابه عند ربه إنه لا يفلح الكافرون وقل رب اغفر وارحم وأنت خير الراحمين"
        m13506 = matcher.match(t13506)
        self.assertEqual(m13506.surah, 23)
        self.assertEqual(m13506.ayah_start, 78)
        self.assertEqual(m13506.ayah_end, 118)
        self.assertGreaterEqual(m13506.confidence, 0.90)

