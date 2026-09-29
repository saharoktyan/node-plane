"""Rich Message screen construction; never use Markdown parse mode."""
from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import (InputRichBlockDetails, InputRichBlockParagraph,
    InputRichBlockSectionHeading, InputRichMessage)


@dataclass(frozen=True)
class Screen:
    title: str
    lines: tuple[str, ...] = ()
    details_title: str | None = None
    details_lines: tuple[str, ...] = ()

    def rich(self) -> InputRichMessage:
        blocks = [InputRichBlockSectionHeading(text=self.title, size=2)]
        blocks.extend(InputRichBlockParagraph(text=line) for line in self.lines if line)
        if self.details_title and self.details_lines:
            blocks.append(InputRichBlockDetails(summary=self.details_title,
                blocks=[InputRichBlockParagraph(text=line) for line in self.details_lines]))
        return InputRichMessage(blocks=blocks, skip_entity_detection=True)

    def plain(self) -> str:
        lines = [self.title, *self.lines]
        if self.details_title and self.details_lines:
            lines.extend((self.details_title, *self.details_lines))
        return '\n\n'.join(line for line in lines if line)
