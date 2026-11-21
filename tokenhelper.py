from dataclasses import dataclass
import sys
from pathlib import Path
import tiktoken


@dataclass(frozen=True)
class TextAnalysis:
    token_count: int
    letter_count: int

    def as_list(self) -> list[int]:
        return [self.token_count, self.letter_count]


def analyze_text(text: str, encoding_name: str = "o200k_base") -> TextAnalysis:
    enc = tiktoken.get_encoding(encoding_name)
    token_count = len(enc.encode(text))
    letter_count = sum(1 for ch in text if ch.isalpha())
    return TextAnalysis(token_count=token_count, letter_count=letter_count)


def read_file(file_path: str) -> str:
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not path.is_file():
        raise ValueError(f"Given path is not a file: {file_path}")

    return path.read_text(encoding="utf-8")


def main() -> None:
    target_file = sys.argv[1] if len(sys.argv) > 1 else "file.txt"

    try:
        content = read_file(target_file)
        output = analyze_text(content)
        print(f"File : {target_file}")
        print(f"Total Tokens : {output.token_count:,}".replace(",", "."))
        print(f"Total Chars  : {output.letter_count:,}".replace(",", "."))
    except Exception as e:
        print(f"Hata: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()