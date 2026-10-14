"""Convert bounded GitHub release Markdown into native Telegram blocks."""
import re
from aiogram.types import InputRichBlockParagraph, InputRichBlockSectionHeading, RichTextBold, RichTextCode, RichTextUrl

INLINE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)|\*\*([^*]+)\*\*|`([^`]+)`")


def inline(value):
    rich, plain, position = [], [], 0
    for match in INLINE.finditer(value):
        prefix = value[position:match.start()]
        if prefix:
            rich.append(prefix)
            plain.append(prefix)
        label,url,bold,code = match.groups()
        if url:
            rich.append(RichTextUrl(text=label,url=url))
            plain.append(label + ' (' + url + ')')
        elif bold:
            rich.append(RichTextBold(text=bold))
            plain.append(bold)
        else:
            rich.append(RichTextCode(text=code))
            plain.append(code)
        position = match.end()
    if position < len(value):
        rich.append(value[position:])
        plain.append(value[position:])
    return rich or [''], ''.join(plain)


def preview(markdown, limit=1800):
    blocks, plain, used = [], [], 0
    lines = str(markdown).splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line:
            continue
        if line.startswith('```') or line.startswith('~~~'):
            fence = line[:3]
            code = []
            while index < len(lines) and not lines[index].strip().startswith(fence):
                code.append(lines[index])
                index += 1
            index += 1
            text = '\n'.join(code)
            block = InputRichBlockParagraph(text=RichTextCode(text=text))
        else:
            heading = re.match(r'^#{1,6}\s+(.+)$',line)
            table = line.startswith('|') and line.endswith('|')
            if table and index < len(lines) and re.fullmatch(r'[| :\-]+',lines[index].strip()):
                # A table is displayed as individual labelled rows on narrow screens.
                headers = [cell.strip() for cell in line.strip('|').split('|')]
                index += 1
                while index < len(lines) and lines[index].strip().startswith('|'):
                    cells = [cell.strip() for cell in lines[index].strip().strip('|').split('|')]
                    index += 1
                    value = ' · '.join(f'{header}: {cell}' for header,cell in zip(headers,cells))
                    rich,text = inline(value)
                    if used + len(text) + 2 > limit:
                        return tuple(blocks), tuple(plain)
                    blocks.append(InputRichBlockParagraph(text=rich))
                    plain.append(text)
                    used += len(text) + 2
                continue
            value = heading[1] if heading else re.sub(r'^[-*]\s+', '• ',line)
            rich,text = inline(value)
            block = InputRichBlockSectionHeading(text=rich,size=3) if heading else InputRichBlockParagraph(text=rich)
        if not text or used + len(text) + 2 > limit:
            break
        blocks.append(block)
        plain.append(text)
        used += len(text) + 2
    return tuple(blocks),tuple(plain)
