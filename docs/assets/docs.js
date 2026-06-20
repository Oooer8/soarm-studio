const globeIcon = `
  <svg class="language-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
    <circle cx="12" cy="12" r="9"></circle>
    <path d="M3 12h18M12 3c2.4 2.5 3.6 5.5 3.6 9S14.4 18.5 12 21c-2.4-2.5-3.6-5.5-3.6-9S9.6 5.5 12 3Z"></path>
  </svg>`;

const chevronIcon = `
  <svg class="language-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
    <path d="m7 10 5 5 5-5" stroke-linecap="round" stroke-linejoin="round"></path>
  </svg>`;

const checkIcon = `
  <svg class="language-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
    <path d="m5 12 4 4L19 7" stroke-linecap="round" stroke-linejoin="round"></path>
  </svg>`;

document.querySelectorAll("select[data-language-switcher]").forEach((select, index) => {
  const selectedOption = select.selectedOptions[0];
  const switcher = document.createElement("div");
  const menuId = `language-menu-${index}`;

  switcher.className = "language-switcher";
  switcher.dataset.languageSwitcher = "";

  const trigger = document.createElement("button");
  trigger.className = "language-trigger";
  trigger.type = "button";
  trigger.setAttribute("aria-label", select.getAttribute("aria-label") || "Language");
  trigger.setAttribute("aria-haspopup", "menu");
  trigger.setAttribute("aria-expanded", "false");
  trigger.setAttribute("aria-controls", menuId);
  trigger.innerHTML = `${globeIcon}<span>${selectedOption?.textContent || "Language"}</span>${chevronIcon}`;

  const menu = document.createElement("div");
  menu.className = "language-menu";
  menu.id = menuId;
  menu.role = "menu";
  menu.hidden = true;

  Array.from(select.options).forEach((option) => {
    const href = option.dataset.href;
    if (!href) {
      return;
    }

    const link = document.createElement("a");
    link.className = "language-option";
    link.href = href;
    link.role = "menuitem";
    link.innerHTML = `<span>${option.textContent}</span>`;

    if (option.selected) {
      link.classList.add("is-current");
      link.setAttribute("aria-current", "page");
      link.insertAdjacentHTML("beforeend", checkIcon);
    }

    menu.appendChild(link);
  });

  switcher.append(trigger, menu);
  select.closest(".language-switcher")?.replaceWith(switcher);

  trigger.addEventListener("click", () => {
    const willOpen = menu.hidden;
    closeLanguageMenus(switcher);
    menu.hidden = !willOpen;
    trigger.setAttribute("aria-expanded", String(willOpen));
  });

  switcher.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) {
      menu.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      trigger.focus();
    }

    if (event.key === "ArrowDown" && document.activeElement === trigger) {
      event.preventDefault();
      menu.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      menu.querySelector(".language-option")?.focus();
    }
  });
});

document.addEventListener("click", (event) => {
  if (!event.target.closest("[data-language-switcher]")) {
    closeLanguageMenus();
  }
});

function closeLanguageMenus(except) {
  document.querySelectorAll("[data-language-switcher]").forEach((switcher) => {
    if (switcher === except) {
      return;
    }

    const trigger = switcher.querySelector(".language-trigger");
    const menu = switcher.querySelector(".language-menu");
    if (trigger && menu) {
      menu.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
    }
  });
}

const copyLabels = document.documentElement.lang.startsWith("zh")
  ? { copied: "已复制", selected: "已选中" }
  : { copied: "Copied", selected: "Selected" };

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
      button.textContent = copyLabels.copied;
    } catch {
      selectCode(code);
      button.textContent = copyLabels.selected;
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
