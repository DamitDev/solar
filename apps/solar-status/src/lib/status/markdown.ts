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

// The public page is client-rendered (`ssr = false`), so the sanitizer is only
// needed in the browser. Creating it lazily keeps this module importable in a
// Node/SSR environment, where `window` does not exist.
let sanitizer: ReturnType<typeof DOMPurify> | null = null;

function getSanitizer(): ReturnType<typeof DOMPurify> {
  if (sanitizer === null) {
    sanitizer = DOMPurify(window);
    sanitizer.addHook('afterSanitizeAttributes', (node) => {
      if (node.tagName === 'A' && node.hasAttribute('href')) {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener noreferrer');
      }
    });
  }
  return sanitizer;
}

/** Render a notice body (markdown) to sanitized HTML safe for `{@html}`. */
export function renderMarkdown(body: string): string {
  const html = parser.parse(body, { async: false });
  if (typeof window === 'undefined') {
    // No DOM to sanitize against: render nothing rather than raw HTML.
    return '';
  }
  return getSanitizer().sanitize(html, { ALLOWED_TAGS, ALLOWED_ATTR });
}
