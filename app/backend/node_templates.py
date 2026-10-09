"""Shared node templates. Draft numbers are previews; allocation is transactional."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class NodeTemplate:
    code: str
    title: str
    region: str
    flag: str

    def draft(self, existing_keys):
        numbers = []
        for key in existing_keys:
            match = re.fullmatch(re.escape(self.code) + r'(\d+)(?:-[a-f0-9]+)?', key)
            if match:
                numbers.append(int(match[1]))
        number = max(numbers, default=0) + 1
        return {'key': f'{self.code}{number}',
                'title': f'{self.title} #{number}', 'region': self.region,
                'flag': self.flag, 'template': self.code}


NODE_TEMPLATES = (
    NodeTemplate('lv', 'Latvia', 'Europe', '🇱🇻'),
    NodeTemplate('de', 'Germany', 'Europe', '🇩🇪'),
    NodeTemplate('nl', 'Netherlands', 'Europe', '🇳🇱'),
    NodeTemplate('fi', 'Finland', 'Europe', '🇫🇮'),
    NodeTemplate('sg', 'Singapore', 'Asia', '🇸🇬'),
    NodeTemplate('us', 'United States', 'North America', '🇺🇸'),
)
