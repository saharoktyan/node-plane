"""Rich Message screen construction; never use Markdown parse mode."""
from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import (InputRichBlockDetails, InputRichBlockParagraph,
    InputRichBlockSectionHeading, InputRichMessage, InputRichBlockPhoto,
    InputRichBlockDocument, InputMediaPhoto, InputMediaDocument, BufferedInputFile,
    RichTextCode, MessageEntity)
from aiogram.types import InputRichBlockButtons, RichMessageButton, InlineKeyboardButton


def rich_buttons(rows, *, navigation=False):
    return [InputRichBlockButtons(buttons=[RichMessageButton(
        text=button.text, callback_data=button.callback_data, url=button.url,
        copy_text=button.copy_text, style='link' if navigation and button.callback_data else button.style)
        for button in row]) for row in rows if row]


@dataclass(frozen=True)
class Section:
    title: str
    lines: tuple[str, ...] = ()
    rows: tuple[tuple[InlineKeyboardButton, ...], ...] = ()
    collapsed: bool = False

    def rich(self):
        blocks = [InputRichBlockParagraph(text=line) for line in self.lines if line]
        blocks.extend(rich_buttons(self.rows))
        if self.collapsed:
            return [InputRichBlockDetails(summary=self.title, blocks=blocks, is_open=False)]
        return [InputRichBlockSectionHeading(text=self.title, size=3), *blocks]


@dataclass(frozen=True)
class Screen:
    title: str
    lines: tuple[str, ...] = ()
    details_title: str | None = None
    details_lines: tuple[str, ...] = ()
    uri: str | None = None
    qr: bytes | None = None
    qr_title: str | None = None
    files: tuple[tuple[str, bytes], ...] = ()
    uri_title: str | None = None
    sections: tuple[Section, ...] = ()
    embedded_buttons: bool = False
    navigation: bool = False

    def rich(self, rows=()) -> InputRichMessage:
        blocks = [InputRichBlockSectionHeading(text=self.title, size=2)]
        blocks.extend(InputRichBlockParagraph(text=line) for line in self.lines if line)
        for section in self.sections:
            blocks.extend(section.rich())
        if self.qr and self.qr_title:
            blocks.append(InputRichBlockDetails(summary=self.qr_title, is_open=False,
                blocks=[InputRichBlockPhoto(photo=InputMediaPhoto(
                    media=BufferedInputFile(self.qr, 'config.png')))]))
        if self.uri:
            uri = InputRichBlockParagraph(text=RichTextCode(text=self.uri))
            blocks.append(InputRichBlockDetails(summary=self.uri_title, blocks=[uri], is_open=False)
                          if self.uri_title else uri)
        blocks.extend(InputRichBlockDocument(document=InputMediaDocument(
            media=BufferedInputFile(content, filename))) for filename, content in self.files)
        if self.details_title and self.details_lines:
            blocks.append(InputRichBlockDetails(summary=self.details_title,
                blocks=[InputRichBlockParagraph(text=line) for line in self.details_lines]))
        if self.embedded_buttons:
            blocks.extend(rich_buttons(rows, navigation=self.navigation))
        return InputRichMessage(blocks=blocks, skip_entity_detection=True)

    def plain(self) -> str:
        lines = [self.title, *self.lines]
        for section in self.sections:
            lines.extend((section.title, *section.lines))
        if self.uri:
            lines.append(self.uri)
        if self.details_title and self.details_lines:
            lines.extend((self.details_title, *self.details_lines))
        return '\n\n'.join(line for line in lines if line)

    def fallback_rows(self, rows):
        return [list(row) for section in self.sections for row in section.rows] + list(rows)

    def plain_entities(self) -> list[MessageEntity] | None:
        if not self.uri:
            return None
        text = self.plain()
        offset = len(text[:text.index(self.uri)].encode('utf-16-le')) // 2
        return [MessageEntity(type='code', offset=offset,
            length=len(self.uri.encode('utf-16-le')) // 2)]
