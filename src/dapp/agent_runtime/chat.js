function setupAgentChat(panel, send) {
  const form = panel.querySelector("form");
  const input = form.elements.message;
  const button = form.querySelector('button[type="submit"]');
  const log = panel.querySelector(".agent-conversation");
  const status = panel.querySelector(".agent-chat-status");
  let history = [];
  let pending;

  function add(role, text, steps = []) {
    const entry = document.createElement("article");
    entry.className = "agent-turn";
    const label = document.createElement("strong");
    label.textContent = role;
    const body = document.createElement("p");
    body.textContent = text;
    entry.append(label, body);
    if (steps.length) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "Tools called (" + steps.length + ")";
      const list = document.createElement("ul");
      for (const step of steps) {
        const line = document.createElement("li");
        line.textContent = step.server + " / " + step.tool + (step.ok ? "" : " · failed");
        list.append(line);
      }
      details.append(summary, list);
      entry.append(details);
    }
    log.append(entry);
    log.scrollTop = log.scrollHeight;
  }

  function reset() {
    pending?.abort();
    pending = undefined;
    history = [];
    log.replaceChildren();
    status.textContent = "";
    button.disabled = false;
  }

  panel.querySelector("[data-clear]").addEventListener("click", reset);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const content = input.value.trim();
    if (!content || pending) return;
    const controller = new AbortController();
    pending = controller;
    const messages = [...history.slice(-18), {role: "user", content}];
    add("You", content);
    input.value = "";
    button.disabled = true;
    status.textContent = "Running agent…";
    try {
      const result = await send(messages, controller.signal);
      if (controller.signal.aborted) return;
      history = [...messages, {role: "assistant", content: result.answer}];
      add("Agent", result.answer, result.steps);
      status.textContent = "";
    } catch (error) {
      if (controller.signal.aborted) return;
      status.textContent = error.message;
      input.value = content;
    } finally {
      if (pending === controller) {
        pending = undefined;
        button.disabled = false;
      }
    }
  });
  return reset;
}

async function agentJson(url, options = {}) {
  const response = await fetch(url, options);
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(response.status === 401 ? "Sign in again to continue." : "Could not reach the agent API.");
  }
  if (!response.ok) {
    const reason = payload.error || payload.detail;
    if (Array.isArray(reason)) {
      throw new Error(reason.map(item => item.msg).join(". "));
    }
    throw new Error(typeof reason === "string" ? reason : "The agent request failed.");
  }
  return payload;
}
