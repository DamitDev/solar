import DOMPurify from 'dompurify';
import { Marked } from 'marked';

/**
 * Notice bodies are maintainer-authored markdown rendered on a public page, so
 * the output is restricted twice: the parser drops what the page must not show
 * (images, h1/h2 headings) and DOMPurify strips everything outside a small
 * allow-list before the HTML reaches `{@html}`.
 */
const parser = new Marked({
  gfm: true,
  breaks: true,
  renderer: {
    // Headings are demoted to h3 so a notice never adds a second h1/h2 under
    // the page title.
    heading({ tokens }) {
      return `<h3>${this.parser.parseInline(tokens)}</h3>\n`;
    },
    // A public status page must not load third-party images.
    image() {
      return '';
    }
  }
});

const ALLOWED_TAGS = [
  'a',
  'blockquote',
  'br',
  'code',
  'del',
  'em',
  'h3',
  'hr',
  'li',
  'ol',
  'p',
  'pre',
  'strong',
  'table',
  'tbody',
  'td',
  'th',
  'thead',
  'tr',
  'ul'
];
const ALLOWED_ATTR = ['href', 'title', 'target', 'rel'];

const sanitizer = DOMPurify(window);
sanitizer.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A' && node.hasAttribute('href')) {
    node.setAttribute('target', '_blank');
    node.setAttribute('rel', 'noopener noreferrer');
  }
});

/** Render a notice body (markdown) to sanitized HTML safe for `{@html}`. */
export function renderMarkdown(body: string): string {
  const html = parser.parse(body, { async: false });
  return sanitizer.sanitize(html, { ALLOWED_TAGS, ALLOWED_ATTR });
}
