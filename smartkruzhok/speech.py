import json
import os
import sys

from faster_whisper import WhisperModel


def main(path):
    model = WhisperModel(os.getenv("WHISPER_MODEL", "base"), device="cpu", compute_type="int8",
                         cpu_threads=4, download_root=os.getenv("WHISPER_CACHE"))
    segments, _ = model.transcribe(
        path, language="ru", word_timestamps=True, vad_filter=True, condition_on_previous_text=False,
    )
    print(json.dumps([
        {"start": segment.start, "end": segment.end, "text": segment.text,
         "words": [(word.start, word.end, word.word) for word in segment.words or ()]}
        for segment in segments
    ]))


if __name__ == "__main__":
    main(sys.argv[1])
