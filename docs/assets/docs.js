document.querySelectorAll(".copy-button").forEach((button) => {
  const originalLabel = button.textContent;

  button.addEventListener("click", async () => {
    const block = button.closest(".cli-block");
    const code = block?.querySelector("code");
    const text = code?.textContent?.trim();
    if (!text) {
      return;
    }

    try {
      await copyText(text);
      button.textContent = "Copied";
    } catch {
      selectCode(code);
      button.textContent = "Selected";
    }

    window.setTimeout(() => {
      button.textContent = originalLabel;
    }, 1400);
  });
});

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Continue to the textarea fallback below.
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.focus({ preventScroll: true });
  textarea.select();

  try {
    if (!document.execCommand("copy")) {
      throw new Error("Copy command failed");
    }
  } finally {
    document.body.removeChild(textarea);
  }
}

function selectCode(code) {
  const selection = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(code);
  selection.removeAllRanges();
  selection.addRange(range);
}
