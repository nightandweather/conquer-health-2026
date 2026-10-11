"""Render synthetic Markdown and check that model text cannot create active HTML."""
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from urllib.parse import urlsplit


PAGE = Path(__file__).resolve().parents[1] / 'local_tools' / 'dialogue.html'
NODE = shutil.which('node')


class ReaderHTML(HTMLParser):
    """Validate generated tags and attributes rather than escaped substrings."""
    tags = {'p', 'br', 'code', 'pre', 'strong', 'em', 'del', 'a', 'blockquote',
            'ul', 'ol', 'li', 'hr', 'div', 'table', 'thead', 'tbody', 'tr', 'th',
            'td', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.seen, self.links, self.text = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.tags:
            raise AssertionError('active or unsupported tag: ' + tag)
        values = dict(attrs)
        permitted = {'a': {'href', 'target', 'rel'}, 'div': {'class'},
                     'ol': {'start'}, 'th': {'style'}, 'td': {'style'}}.get(tag, set())
        if not set(values).issubset(permitted):
            raise AssertionError('unexpected attribute: ' + repr(attrs))
        if tag == 'a':
            if urlsplit(values['href']).scheme not in {'http', 'https', 'mailto'}:
                raise AssertionError('unsafe link: ' + values['href'])
            if values.get('target') != '_blank' or values.get('rel') != 'noopener noreferrer':
                raise AssertionError('external link missing protections')
            self.links.append(values['href'])
        if tag in {'th', 'td'} and values.get('style') not in {
                'text-align:left', 'text-align:right', 'text-align:center'}:
            raise AssertionError('unsafe table style')
        if tag == 'div' and values.get('class') != 'table-scroll':
            raise AssertionError('unexpected wrapper')
        self.seen.append(tag)

    def handle_data(self, value):
        self.text.append(value)


@unittest.skipUnless(NODE, 'Node.js is needed for the local browser renderer checks')
class MarkdownReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        html = PAGE.read_text(encoding='utf-8')
        cls.renderer = re.search(r'<script id="markdown-renderer">(.*?)</script>', html,
                                 re.S).group(1)

    def render_many(self, inputs):
        # Evaluate precisely the renderer shipped in the offline page, without a DOM.
        script = self.renderer + '\nconst inputs = ' + json.dumps(inputs) + ';\n' + (
            'process.stdout.write(JSON.stringify(inputs.map(value => renderMarkdown(value))));')
        completed = subprocess.run([NODE, '-e', script], capture_output=True, text=True,
                                   check=True, timeout=10)
        results = json.loads(completed.stdout)
        for output in results:
            parser = ReaderHTML()
            parser.feed(output)
        return results

    def test_common_answer_structures_are_readable(self):
        output, = self.render_many(['# 요약\n\n**중요**한 사실과 *주의* 사항.\n\n'
            '- 항목 하나\n- 항목 둘\n  - 하위 항목\n\n'
            '3. 세 번째\n4. 네 번째\n\n> 인용문\n\n'
            '| 항목 | 값 |\n| :--- | ---: |\n| 증상 | `a|b` |\n\n'
            '```python\nprint("hello")\n```\n\n'
            '[근거](https://example.org/evidence)'])
        parser = ReaderHTML()
        parser.feed(output)
        self.assertTrue({'h1', 'strong', 'em', 'ul', 'ol', 'li', 'blockquote',
                         'table', 'th', 'td', 'pre', 'code', 'a'}.issubset(parser.seen))
        self.assertIn('<ol start="3">', output)
        self.assertIn('<code>a|b</code>', output)
        self.assertEqual(parser.links, ['https://example.org/evidence'])

    def test_raw_html_and_images_cannot_execute_or_fetch(self):
        examples = ['<script>alert(1)</script>', '<img src="https://example.org/tracker" onerror="alert(1)">',
                    '<svg><a xlink:href="javascript:alert(1)">x</a></svg>',
                    '![tracker](https://example.org/tracker)',
                    '**<iframe src="https://example.org">**',
                    '```html\n<script>alert(1)</script>\n```',
                    '`<img onerror=alert(1)>`']
        outputs = self.render_many(examples)
        for output in outputs:
            parser = ReaderHTML()
            parser.feed(output)
            self.assertFalse(parser.links)
        self.assertIn('&lt;script&gt;', outputs[0])
        self.assertIn('tracker', outputs[3])

    def test_unsafe_link_schemes_and_attribute_injection_are_inert(self):
        examples = ['[x](javascript:alert(1))', '[x](JaVaScRiPt:alert(1))',
                    '[x](data:text/html;base64,SGk=)', '[x](file:///etc/passwd)',
                    '[x](//example.org)', '[x](java\nscript:alert(1))',
                    '[x](javascript&colon;alert(1))', '<javascript:alert(1)>',
                    '[x](https://example.org/\"onmouseover=\"alert(1))',
                    '[<img onerror=alert(1)>](https://example.org/)']
        for output in self.render_many(examples):
            parser = ReaderHTML()
            parser.feed(output)
            self.assertNotIn('script', parser.seen)
            self.assertNotIn('img', parser.seen)
        for output in self.render_many(examples[:8]):
            parser = ReaderHTML()
            parser.feed(output)
            self.assertFalse(parser.links)

    def test_partial_streamed_markdown_stays_safe(self):
        source = '# 진행 중\n\n**강조**와 [근거](https://example.org/a(b))\n\n'
        source += '| 분류 | 내용 |\n| --- | --- |\n| A | <img onerror=alert(1)> |\n\n'
        source += '```html\n<script>alert(1)</script>\n```'
        outputs = self.render_many([source[:end] for end in range(len(source) + 1)])
        self.assertEqual(len(outputs), len(source) + 1)

    def test_newlines_and_literal_code_preserve_answer_content(self):
        output, = self.render_many(['제목\r\n====\r\n\r\n첫 문단\r\n다음 줄\r\n\r\n'
                                   '    <code>& text\r\n'])
        self.assertIn('<h1>제목</h1>', output)
        self.assertIn('첫 문단<br>다음 줄', output)
        self.assertIn('&lt;code&gt;&amp; text', output)

    def test_model_table_breaks_are_readable_without_allowing_attributes(self):
        output, = self.render_many(['| 항목 | 내용 |\n| --- | --- |\n'
                                   '| A | 하나<br>둘<br/>셋<br />넷 |\n\n'
                                   '<br onmouseover="alert(1)">'])
        self.assertIn('하나<br>둘<br>셋<br>넷', output)
        self.assertIn('&lt;br onmouseover=', output)


if __name__ == '__main__':
    unittest.main()
