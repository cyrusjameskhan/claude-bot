"""
Voice message transcription using Faster-Whisper.
Handles downloading, converting, and transcribing audio from Telegram.
"""

import asyncio
import tempfile
from pathlib import Path
from typing import Optional, Tuple
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class TranscriptionResult:
    """Result of a voice transcription."""
    success: bool
    text: Optional[str]
    language: Optional[str]
    duration_seconds: Optional[float]
    error: Optional[str] = None


class WhisperTranscriber:
    """
    Transcribes audio using Faster-Whisper model.
    Loads the model lazily on first use to save memory.
    """
    
    def __init__(self, model_name: str = "base", device: str = "cpu"):
        self.model_name = model_name
        self.device = device
        self._model = None
        self._model_lock = asyncio.Lock()
    
    async def _ensure_model_loaded(self) -> None:
        """Load the Whisper model if not already loaded."""
        if self._model is not None:
            return
        
        async with self._model_lock:
            # Double-check after acquiring lock
            if self._model is not None:
                return
            
            logger.info(f"Loading Faster-Whisper model '{self.model_name}' on {self.device}...")
            
            # Run model loading in thread pool (it's CPU-bound)
            loop = asyncio.get_event_loop()
            self._model = await loop.run_in_executor(
                None, self._load_model_sync
            )
            
            logger.info("Whisper model loaded successfully")
    
    def _load_model_sync(self):
        """Synchronously load the Whisper model."""
        from faster_whisper import WhisperModel
        
        # For CPU, use int8 for faster inference
        compute_type = "int8" if self.device == "cpu" else "float16"
        
        return WhisperModel(
            self.model_name, 
            device=self.device,
            compute_type=compute_type
        )
    
    async def transcribe(self, audio_path: Path) -> TranscriptionResult:
        """
        Transcribe an audio file.
        
        Args:
            audio_path: Path to the audio file (supports various formats).
            
        Returns:
            TranscriptionResult with the transcribed text or error.
        """
        try:
            await self._ensure_model_loaded()
            
            if not audio_path.exists():
                return TranscriptionResult(
                    success=False,
                    text=None,
                    language=None,
                    duration_seconds=None,
                    error=f"Audio file not found: {audio_path}"
                )
            
            # Run transcription in thread pool
            loop = asyncio.get_event_loop()
            segments, info = await loop.run_in_executor(
                None, 
                self._transcribe_sync, 
                str(audio_path)
            )
            
            # Combine all segments into full text
            full_text = " ".join([segment.text.strip() for segment in segments])
            
            return TranscriptionResult(
                success=True,
                text=full_text,
                language=info.language,
                duration_seconds=info.duration,
                error=None
            )
            
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return TranscriptionResult(
                success=False,
                text=None,
                language=None,
                duration_seconds=None,
                error=str(e)
            )
    
    def _transcribe_sync(self, audio_path: str):
        """Synchronously transcribe audio."""
        segments, info = self._model.transcribe(
            audio_path,
            beam_size=5,
            vad_filter=True  # Filter out silence
        )
        # Convert generator to list to materialize results
        return list(segments), info


class AudioProcessor:
    """Handles audio file downloading and format conversion."""
    
    def __init__(self, temp_dir: Optional[Path] = None):
        self.temp_dir = temp_dir or Path(tempfile.gettempdir()) / "telegram_claude_bot"
        self.temp_dir.mkdir(exist_ok=True)
    
    async def download_telegram_voice(self, bot, file_id: str) -> Tuple[Optional[Path], Optional[str]]:
        """
        Download a voice message from Telegram.
        
        Args:
            bot: Telegram bot instance.
            file_id: Telegram file ID.
            
        Returns:
            Tuple of (file_path, error_message).
        """
        try:
            file = await bot.get_file(file_id)
            
            # Telegram voice messages are in OGG format
            file_path = self.temp_dir / f"{file_id}.ogg"
            
            await file.download_to_drive(file_path)
            
            logger.info(f"Downloaded voice message to {file_path}")
            return file_path, None
            
        except Exception as e:
            logger.error(f"Failed to download voice message: {e}")
            return None, str(e)
    
    async def convert_to_wav(self, input_path: Path) -> Tuple[Optional[Path], Optional[str]]:
        """
        Convert audio file to WAV format for better Whisper compatibility.
        
        Args:
            input_path: Path to input audio file.
            
        Returns:
            Tuple of (output_path, error_message).
        """
        try:
            from pydub import AudioSegment
            
            output_path = input_path.with_suffix(".wav")
            
            # Run conversion in thread pool
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                self._convert_sync,
                input_path,
                output_path
            )
            
            return output_path, None
            
        except Exception as e:
            logger.error(f"Audio conversion failed: {e}")
            return None, str(e)
    
    def _convert_sync(self, input_path: Path, output_path: Path) -> None:
        """Synchronously convert audio."""
        from pydub import AudioSegment
        
        audio = AudioSegment.from_file(str(input_path))
        audio.export(str(output_path), format="wav")
    
    def cleanup(self, *paths: Path) -> None:
        """Remove temporary files."""
        for path in paths:
            try:
                if path and path.exists():
                    path.unlink()
            except Exception as e:
                logger.warning(f"Failed to cleanup {path}: {e}")


class VoiceHandler:
    """
    High-level interface for handling voice messages.
    Combines downloading, conversion, and transcription.
    """
    
    def __init__(self, transcriber: WhisperTranscriber, processor: Optional[AudioProcessor] = None):
        self.transcriber = transcriber
        self.processor = processor or AudioProcessor()
    
    async def process_voice_message(self, bot, file_id: str) -> TranscriptionResult:
        """
        Process a Telegram voice message end-to-end.
        
        Args:
            bot: Telegram bot instance.
            file_id: Telegram file ID of the voice message.
            
        Returns:
            TranscriptionResult with the transcribed text.
        """
        ogg_path = None
        wav_path = None
        
        try:
            # Download the voice message
            ogg_path, error = await self.processor.download_telegram_voice(bot, file_id)
            if error:
                return TranscriptionResult(
                    success=False,
                    text=None,
                    language=None,
                    duration_seconds=None,
                    error=f"Download failed: {error}"
                )
            
            # Faster-whisper can handle OGG directly
            result = await self.transcriber.transcribe(ogg_path)
            
            if not result.success and "format" in (result.error or "").lower():
                # Try converting to WAV
                wav_path, convert_error = await self.processor.convert_to_wav(ogg_path)
                if convert_error:
                    return TranscriptionResult(
                        success=False,
                        text=None,
                        language=None,
                        duration_seconds=None,
                        error=f"Conversion failed: {convert_error}"
                    )
                result = await self.transcriber.transcribe(wav_path)
            
            return result
            
        finally:
            # Cleanup temporary files
            self.processor.cleanup(ogg_path, wav_path)


def create_voice_handler(settings) -> VoiceHandler:
    """Factory function to create VoiceHandler from settings."""
    transcriber = WhisperTranscriber(
        model_name=settings.whisper_model,
        device=settings.whisper_device
    )
    return VoiceHandler(transcriber)
