/**
 * Debug wrapper for OpenAI API fetch requests.
 * Logs detailed error info (especially for 400 Bad Request).
 */
export async function fetchWithDiagnostics(url, options) {
  try {
    const res = await fetch(url, options);

    if (res.ok) return res;

    let errorBody = await res.text();
    try {
      const json = JSON.parse(errorBody);
      console.group("OpenAI API Error Diagnostic");
      console.error("Status:", res.status, res.statusText);
      console.error("Message:", json.error?.message || json);
      console.error("Type:", json.error?.type || "unknown");
      console.error("Param:", json.error?.param || "n/a");
      console.groupEnd();
    } catch {
      console.error("Non-JSON error response:", errorBody);
    }

    throw new Error(`OpenAI API returned ${res.status}`);
  } catch (err) {
    console.error("fetchWithDiagnostics error:", err.message);
    throw err;
  }
}

// Markdown to PDF (simple text renderer)
export async function markdownToPDFBlob(markdownText) {
  // Preferred: pdfmake + html-to-pdfmake for clean, selectable text
  if (window.pdfMake && window.htmlToPdfmake && globalThis.marked) {
    try {
      const collapseSpacedOut = (str) => {
        try { return str.replace(/((?:\p{L}\s){3,}\p{L})/gu, (m) => m.replace(/\s+/g, "")); } catch { return str; }
      };
      const clean = (s) => {
        let t = String(s || "");
        // t = t.replace(/?cite?[\s\S]*??/g, "");
        t = t.replace(/[\uE000-\uF8FF]/g, "");
        t = t.replace(/[\u200B-\u200D\uFEFF\u200E\u200F]/g, "");
        t = t.replace(/[\u00A0\u2000-\u200A\u202F\u205F]/g, " ");
        // Remove stray ampersands not part of HTML entities
       //  t = t.replace(/&(?!amp;|lt;|gt;|quot;|apos;|nbsp;|#[0-9]+;|#x[0-9A-Fa-f]+;)/g, "");        
        t = t.replace(/&(?!amp;|lt;|gt;|quot;|apos;|nbsp;|#[0-9]+;|#x[0-9A-Fa-f]+;)/g, "");
        // Drop &amp; when interleaved between alphanumerics
        t = t.replace(/([A-Za-z0-9])&amp;(?=[A-Za-z0-9])/g, "$1");
        t = t.replace(/(?<=[A-Za-z0-9])&amp;([A-Za-z0-9])/g, "$1");
        t = collapseSpacedOut(t);
        return t;
      };
      const rawHtml = globalThis.marked.parse(clean(markdownText));
      const safeHtml = window.DOMPurify ? window.DOMPurify.sanitize(rawHtml) : rawHtml;
      const pdfContent = window.htmlToPdfmake(safeHtml, { window });
      const docDefinition = {
        pageSize: 'LETTER',
        pageMargins: [40, 48, 40, 48],
        defaultStyle: { fontSize: 11 },
        content: pdfContent,
      };
      return await new Promise((resolve, reject) => {
        try { window.pdfMake.createPdf(docDefinition).getBlob((b) => resolve(b)); }
        catch (e) { reject(e); }
      });
    } catch (e) {
      console.warn('pdfmake generation failed, falling back to simple renderer:', e);
    }
  }
  
  // Fallback: simple text rendering via jsPDF
  try {
    const { jsPDF } = window.jspdf;
    const pdf = new jsPDF({ unit: "pt", format: "letter" });

    // Revert to text-based rendering for reliability, with manual word-wrapping
    const html = (globalThis.marked?.parse ? globalThis.marked.parse(markdownText) : markdownText);
    const tempDiv = document.createElement("div");
    tempDiv.innerHTML = html;

    const pageWidth = pdf.internal.pageSize.getWidth();
    const pageHeight = pdf.internal.pageSize.getHeight();
    const margin = 40;
    const usableWidth = pageWidth - margin * 2;
    const lineHeight = 18;
    let y = margin;

    const raw = (tempDiv.innerText || String(markdownText || "")).replace(/\r/g, "");
    const lines = raw.split("\n");

    const writeLine = (text, x) => {
      if (y > pageHeight - margin) {
        pdf.addPage();
        y = margin;
      }
      pdf.text(text, x, y);
      y += lineHeight;
    };

    const wrapAndWrite = (text, indent = 0) => {
      const x = margin + indent;
      const words = text.split(/\s+/).filter(Boolean);
      let line = "";
      const space = " ";
      for (const w of words) {
        const test = line ? line + space + w : w;
        if (pdf.getTextWidth(test) > (usableWidth - indent)) {
          if (line) writeLine(line, x);
          line = w;
        } else {
          line = test;
        }
      }
      if (line) writeLine(line, x);
    };

    for (let line of lines) {
      const trimmed = line.trim();
      if (!trimmed) {
        // Blank line = paragraph break
        y += lineHeight / 2;
        continue;
      }

      // Detect headings to add emphasis (simple: add blank line before)
      const isHeading = /^(#{1,6}|==+|--+)/.test(trimmed) || /^[A-Z][A-Za-z0-9 \-]{0,50}$/.test(trimmed);
      const bulletMatch = /^([\-*•])\s+(.+)$/.exec(trimmed);
      const orderedMatch = /^(\d+[\.)])\s+(.+)$/.exec(trimmed);

      if (isHeading) y += lineHeight / 2;

      if (bulletMatch) {
        // Unordered list item
        const content = bulletMatch[2];
        wrapAndWrite(`• ${content}`, 12);
      } else if (orderedMatch) {
        // Ordered list item
        const num = orderedMatch[1].replace(/\.$/, ".");
        wrapAndWrite(`${num} ${orderedMatch[2]}`, 12);
      } else {
        wrapAndWrite(trimmed, 0);
      }
    }

    return pdf.output("blob");
  } catch (err) {
    console.error("Failed to generate PDF blob:", err);
    throw err;
  }
}

// Add a PDF download button to the last assistant message
export async function renderMarkdownPDFDownload(markdown, containerSelector = "#chat-history") {
  const pdfBlob = await markdownToPDFBlob(markdown);
  const pdfURL = URL.createObjectURL(pdfBlob);

  // Build a friendly filename from first heading if present
  let filename = "assistant_output.pdf";
  const m = /^\s{0,3}#{1,6}\s+(.+)$/m.exec(markdown || "");
  if (m && m[1]) {
    const slug = m[1]
      .replace(/[*_`~\[\]#]+/g, "")
      .trim()
      .slice(0, 60)
      .replace(/\s+/g, "-")
      .replace(/[^\w\-]+/g, "");
    if (slug) filename = `${slug}.pdf`;
  }

  // Render as a separate system-style message item
  const chatContainer = document.querySelector(containerSelector);
  if (chatContainer) {
    const wrapper = document.createElement("div");
    wrapper.className = "chat-message system";
    const content = document.createElement("div");
    content.className = "message-content";
    const link = document.createElement("a");
    link.href = pdfURL;
    link.download = filename;
    link.textContent = "Download PDF";
    link.className = "download-link";
    content.appendChild(link);
    wrapper.appendChild(content);
    chatContainer.appendChild(wrapper);
    // Scroll to bottom using same behavior as UI module
    try {
      chatContainer.scrollTop = chatContainer.scrollHeight;
    } catch {}
  }

  return { url: pdfURL, blob: pdfBlob, filename };
}

// Utility for Safe Markdown Conversion (escape/clean text)
export function sanitizeMarkdown(markdown) {
  return markdown
    .replace(/<script[\s\S]*?>[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?>[\s\S]*?<\/style>/gi, "")
    .trim();
}
