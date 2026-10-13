from pathlib import Path
root = Path(__file__).resolve().parent / 'review-1.3.2'
(root / 'extract_subprocess.py').write_text('''"""Compatibility facade for the bounded PDF worker."""
from pdf_worker import (extract_in_subprocess, scrub_environment, CHILD_SCRIPT, CHILD_FLAG,
                        EXTRACT_TIMEOUT_SECONDS, MAX_CHILD_OUTPUT_BYTES)
''', encoding='utf-8')
p = root / 'pdftext.py'
s = p.read_text(encoding='utf-8').replace('EXTRACTOR_VERSION = "2.0.0"', 'EXTRACTOR_VERSION = "2.1.0"')
s = s.replace('MAX_PAGE_OBJECTS = 200000', '''MAX_PAGE_OBJECTS = 200000
MAX_STREAM_BYTES = 16 * 1024 * 1024

def bounded_inflate(raw):
    decoder = zlib.decompressobj()
    expanded = decoder.decompress(raw, MAX_STREAM_BYTES + 1)
    if len(expanded) > MAX_STREAM_BYTES or decoder.unconsumed_tail:
        raise ExtractionError('extract_too_large', 'PDF stream exceeds extraction budget')
    return expanded
''')
s = s.replace('raw = zlib.decompress(raw)', 'raw = bounded_inflate(raw)')
s = s.replace('raw = zlib.decompressobj().decompress(raw)', 'raw = bounded_inflate(raw)')
s = s.replace('raw = self._undo_png_predictor(raw,', 'raw = BuiltinBackend._undo_png_predictor(raw,')
s = s.replace('        blob = Path(path).read_bytes()', '''        if Path(path).stat().st_size > 100 * 1024 * 1024:
            raise ExtractionError('extract_too_large', 'PDF exceeds extraction budget')
        blob = Path(path).read_bytes()''')
p.write_text(s, encoding='utf-8')
p = root / 'extraction.py'
s = p.read_text(encoding='utf-8')
s += '''

class IsolatedPdfTextExtractor(PdfTextExtractor):
    """All untrusted parsing, backend probes and cache verification run in a child.

    Cache is regenerated after resource isolation, so a corrupt local cache cannot
    supply fabricated text to model evidence verification.
    """
    def extract(self, path, paper_id=None, use_cache=None):
        import pdf_worker
        payload, info, _ = pdf_worker.extract_in_subprocess(path, paper_id or '')
        if payload is None:
            return self._failure(path, info.get('code', 'extract_failed'),
                                 'PDF text extraction unavailable; manual reading is available')
        result = ExtractionResult.from_cache_payload(payload, path)
        result.cached = False
        if self.allow_cache if use_cache is None else use_cache:
            self._write_cache(self.cache_key(path, paper_id), result)
        return result
'''
s = s.replace('    extractor = PdfTextExtractor(files=files, **kwargs)',
              '    extractor = IsolatedPdfTextExtractor(files=files, **kwargs)')
p.write_text(s, encoding='utf-8')
p = root / 'analysis.py'
s = p.read_text(encoding='utf-8')
s = s.replace('lambda: PdfTextExtractor(files=self.files)',
              'lambda: __import__("extraction").IsolatedPdfTextExtractor(files=self.files)')
p.write_text(s, encoding='utf-8')
p = root / 'backup_guard.py'
s = p.read_text(encoding='utf-8').replace('from extraction import PdfTextExtractor',
                                        'from extraction import IsolatedPdfTextExtractor')
s = s.replace('lambda: PdfTextExtractor(allow_cache=False)',
              'lambda: IsolatedPdfTextExtractor(allow_cache=False)')
p.write_text(s, encoding='utf-8')
print('Bounded PDF parsing fixes applied')
