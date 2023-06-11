import logging
import json

from typing import Optional, Dict


DEFAULT_I18N_FILE = 'config/i18n/en.json'

log = logging.getLogger(__name__)


class I18N:
    def __init__(self, i18n_file: str) -> None:
        log.debug('Init JSON obj with {}'.format(i18n_file))
        self.file = i18n_file
        self.data = self.parse(i18n_file)
        self.default = self.parse(DEFAULT_I18N_FILE)

    def parse(self, file: str) -> Dict[str, str]:
        """
        Parse the file as JSON
        """
        with open(file, 'r', encoding='utf-8') as data:
            try:
                parsed = json.load(data)
            except Exception:
                log.error(
                    'Error parsing {} as JSON'.format(self.file),
                    exc_info=True
                )
                parsed = {}
        return parsed

    def get(self, key: str, fallback: Optional[str] = None) -> str:
        """
        Gets a string from a i18n file
        """
        try:
            data = self.data[key]
        except KeyError:
            data = fallback if (fallback) else self.default[key]
            log.warning(
                'Could not grab data from i18n key {}.'.format(key)
            )
        return data
