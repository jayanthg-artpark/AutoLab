// Chat with the AI assistant: transcript, composer with attachments, streamed replies.

import { request, readEvents, ApiError } from "./api.js";
import { $, h, icon, markdown } from "./dom.js";

const SUGGESTIONS = [
  "Two-fold serial dilution of a dye stock across columns 1-11 of a 96-well plate (column 12 blank), 3 replicate rows, then read absorbance at 600 nm.",
  "Set up a Bradford assay: BSA standards 0-2000 µg/mL in duplicate plus 8 unknowns, 5 µL sample + 250 µL dye, read at 595 nm.",
  "Plan a sandwich ELISA on a pre-coated plate: standards and 20 samples in duplicate, with washes between steps.",
  "Design the experiment in the attached PDF and tell me what the lab needs to prepare.",
];
// The model's name as the server reports it (Claude, Gemini, Ollama...).
const assistantName = () => document.querySelector(".assistant-name")?.dataset.label || "The assistant";

const TOOL_LABELS = {
  record_requirements: "Noting the requirements…",
  search_knowledge: "Looking it up…",
  read_knowledge: "Reading the manual…",
  write_protocol: "Writing the protocol…",
  set_workflow: "Planning the workflow…",
  set_reader_settings: "Setting up the plate reader…",
};
const RUNNING_LABELS = {
  record_requirements: "Saving the requirements…",
  search_knowledge: "Searching kit manuals and assay templates…",
  read_knowledge: "Reading…",
  write_protocol: "Simulating on the Opentrons simulator…",
  set_workflow: "Checking with the master node…",
  set_reader_settings: "Saving plate-reader settings…",
};

export function createChat({ getSession, onSession, onBusyChange }) {
  const messages = $("messages");
  const input = $("chat-input");
  const fileInput = $("file-input");
  const composer = $("composer");
  let files = [];
  let busy = false;

  function setBusy(value) {
    busy = value;
    $("send-chat").disabled = value;
    $("attach-btn").disabled = value;
    $("new-design").disabled = value;
    onBusyChange(value);
  }

  // --- Rendering -------------------------------------------------------------------

  function render(session) {
    if (busy) return;
    const entries = session?.transcript ?? [];
    if (!entries.length) {
      messages.replaceChildren(welcome());
      return;
    }
    messages.replaceChildren(...entries.map(renderEntry));
    scrollToEnd(true);
  }

  function welcome() {
    return h(
      "div",
      { class: "welcome" },
      h("h3", {}, "What experiment are we running?"),
      h("p", {}, `Describe it, or attach a protocol PDF, plate map or paper. ${assistantName()} works out the requirements (checking the lab's kit manuals and assay templates), plans the workflow across the lab's instruments, and writes and simulates the Opentrons Flex protocols. The panels on the right update as it goes; you review and approve the run before anything goes to the lab.`),
      h(
        "div",
        { class: "suggestions" },
        SUGGESTIONS.map((text) =>
          h("button", { type: "button", class: "suggestion", onclick: () => { input.value = text; autosize(); input.focus(); } }, text),
        ),
      ),
    );
  }

  function renderEntry(entry) {
    if (entry.role === "user") {
      return h(
        "div",
        { class: "msg user" },
        entry.attachments?.length ? h("div", { class: "chips" }, entry.attachments.map(fileChip)) : null,
        entry.text ? h("div", { class: "bubble" }, entry.text) : null,
      );
    }
    const view = assistantView();
    // Older entries have no parts: show their text, then their tool activity.
    const parts = entry.parts?.length
      ? entry.parts
      : [...(entry.text ? [{ type: "text", text: entry.text }] : []), ...entry.tools.map((t) => ({ type: "tool", ...t }))];
    view.setParts(parts);
    if (entry.error) view.setError(entry.error);
    return view.el;
  }

  // An assistant reply: text and tool activity in the order they happened.
  function assistantView() {
    const status = h("div", { class: "status-line" });
    status.hidden = true;
    const body = h("div", { class: "reply-parts" });
    const error = h("div", { class: "msg-error", role: "alert" });
    error.hidden = true;
    const el = h("div", { class: "msg assistant" }, body, status, error);
    let parts = [];
    let frame = 0;

    const draw = () => {
      body.replaceChildren(
        ...parts.map((part) =>
          part.type === "text"
            ? h("div", { class: "bubble md" }, markdown(part.text))
            : h("div", { class: `tool-chip ${part.ok ? "ok" : "bad"}` }, icon(part.ok ? "check" : "warn"), h("span", {}, part.summary)),
        ),
      );
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(() => { frame = 0; draw(); });
    };
    return {
      el,
      setParts(value) {
        parts = value.map((p) => ({ ...p }));
        schedule();
      },
      appendText(delta) {
        const last = parts[parts.length - 1];
        if (last?.type === "text") last.text += delta;
        else parts.push({ type: "text", text: delta.replace(/^\n+/, "") });
        schedule();
      },
      addTool(activity) {
        parts.push({ type: "tool", ...activity });
        schedule();
      },
      setStatus(label) {
        status.hidden = !label;
        status.replaceChildren(...(label ? [icon("spinner"), label] : []));
      },
      setError(message) {
        error.hidden = !message;
        error.textContent = message;
      },
    };
  }

  function fileChip(name, onRemove) {
    return h(
      "span",
      { class: "file-chip" },
      icon("file"),
      name,
      onRemove ? h("button", { type: "button", "aria-label": `Remove ${name}`, onclick: onRemove }, "×") : null,
    );
  }

  function renderAttachments() {
    $("attachments").replaceChildren(
      ...files.map((file, i) => h("li", {}, fileChip(file.name, () => { files.splice(i, 1); renderAttachments(); }))),
    );
  }

  function scrollToEnd(force = false) {
    const nearBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 120;
    if (force || nearBottom) messages.scrollTop = messages.scrollHeight;
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  }

  // --- Sending ------------------------------------------------------------------------

  async function send() {
    const session = getSession();
    const text = input.value.trim();
    if (busy || !session || (!text && !files.length)) return;

    const form = new FormData();
    form.append("message", text);
    files.forEach((file) => form.append("files", file, file.name));

    setBusy(true);
    if (messages.querySelector(".welcome")) messages.replaceChildren();
    const reply = assistantView();
    reply.setStatus("Sending…");
    let replyShown = false;
    const showReply = () => {
      if (!replyShown) {
        messages.append(reply.el);
        replyShown = true;
      }
    };

    try {
      const response = await request(`/api/sessions/${session.id}/chat`, { method: "POST", body: form });
      input.value = "";
      autosize();
      files = [];
      renderAttachments();
      for await (const event of readEvents(response)) {
        switch (event.type) {
          case "entry":
            messages.append(renderEntry(event.entry));
            showReply();
            reply.setStatus("Thinking…");
            break;
          case "thinking":
            reply.setStatus("Thinking…");
            break;
          case "text":
            reply.setStatus("");
            reply.appendText(event.text);
            break;
          case "text_reset":
            reply.setParts(event.parts ?? []);
            break;
          case "tool_start":
            reply.setStatus(TOOL_LABELS[event.name] ?? "Working…");
            break;
          case "tool_running":
            reply.setStatus(RUNNING_LABELS[event.name] ?? "Working…");
            break;
          case "tool_done":
            reply.addTool(event.activity);
            reply.setStatus("Thinking…");
            break;
          case "session":
            onSession(event.session);
            break;
          case "error":
            reply.setError(event.message);
            break;
          case "done":
            reply.setStatus("");
            onSession(event.session);
            break;
        }
        scrollToEnd();
      }
    } catch (err) {
      showReply();
      reply.setStatus("");
      reply.setError(err instanceof ApiError ? err.message : `Connection lost: ${err.message}`);
    } finally {
      reply.setStatus("");
      setBusy(false);
      scrollToEnd();
    }
  }

  // --- Wiring -----------------------------------------------------------------------------

  composer.addEventListener("submit", (e) => {
    e.preventDefault();
    send();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });
  input.addEventListener("input", autosize);
  $("attach-btn").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    files.push(...fileInput.files);
    fileInput.value = "";
    renderAttachments();
  });
  const panel = composer.closest(".chat-panel");
  panel.addEventListener("dragover", (e) => {
    if (busy || !e.dataTransfer?.types.includes("Files")) return;
    e.preventDefault();
    composer.classList.add("dragging");
  });
  panel.addEventListener("dragleave", (e) => {
    if (!panel.contains(e.relatedTarget)) composer.classList.remove("dragging");
  });
  panel.addEventListener("drop", (e) => {
    if (busy || !e.dataTransfer?.files.length) return;
    e.preventDefault();
    composer.classList.remove("dragging");
    files.push(...e.dataTransfer.files);
    renderAttachments();
  });

  return { render, isBusy: () => busy };
}
