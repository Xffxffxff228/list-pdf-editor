import * as pdfjsLib from "./vendor/pdfjs/pdf.mjs";

const $ = (selector) => document.querySelector(selector);
const stage = $("#stage");
const input = $("#fileInput");
const modal = $("#replaceModal");
const MAX_PDF_BYTES = 100 * 1024 * 1024;

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  "./vendor/pdfjs/pdf.worker.mjs",
  import.meta.url,
).toString();

let pdf = null;
let sourceBytes = null;
let fileName = "";
let page = 1;
let zoom = 1;
let baseScale = 1;
let renderTask = null;
let textItems = [];
let pending = null;
let replacementCount = 0;
let renderRevision = 0;
let loadRevision = 0;
let resizeTimer = 0;
let previousFocus = null;

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 3300);
}

function renderMarks() {
  const canvas = $(".layer");
  if (!canvas) return;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  if (!pending) return;
  context.fillStyle = "rgba(64,140,99,.22)";
  context.strokeStyle = "#285d49";
  context.lineWidth = 2 * devicePixelRatio;
  context.fillRect(
    pending.x * canvas.width,
    pending.y * canvas.height,
    pending.w * canvas.width,
    pending.h * canvas.height,
  );
  context.strokeRect(
    pending.x * canvas.width,
    pending.y * canvas.height,
    pending.w * canvas.width,
    pending.h * canvas.height,
  );
}

async function render() {
  if (!pdf) return;
  const revision = ++renderRevision;
  renderTask?.cancel();

  try {
    const currentPage = await pdf.getPage(page);
    if (revision !== renderRevision) return;

    const pageWidth = currentPage.getViewport({ scale: 1 }).width;
    const maxWidth = Math.max(160, Math.min(stage.clientWidth - 60, 900));
    baseScale = Math.min(maxWidth / pageWidth, 1.5);
    const viewport = currentPage.getViewport({ scale: baseScale * zoom });

    stage.replaceChildren();
    const shell = document.createElement("div");
    shell.className = "page-shell";
    shell.style.width = `${viewport.width}px`;
    shell.style.height = `${viewport.height}px`;

    const canvas = document.createElement("canvas");
    const pixelRatio = Math.max(1, Math.min(devicePixelRatio || 1, 2));
    canvas.width = Math.ceil(viewport.width * pixelRatio);
    canvas.height = Math.ceil(viewport.height * pixelRatio);
    canvas.style.width = `${viewport.width}px`;
    canvas.style.height = `${viewport.height}px`;
    shell.append(canvas);

    const layer = document.createElement("canvas");
    layer.className = "layer";
    layer.width = canvas.width;
    layer.height = canvas.height;
    layer.style.width = `${viewport.width}px`;
    layer.style.height = `${viewport.height}px`;
    shell.append(layer);
    stage.append(shell);

    const task = currentPage.render({
      canvasContext: canvas.getContext("2d"),
      viewport,
      transform: pixelRatio !== 1
        ? [pixelRatio, 0, 0, pixelRatio, 0, 0]
        : null,
    });
    renderTask = task;
    try {
      await task.promise;
    } catch (error) {
      if (error.name !== "RenderingCancelledException") throw error;
      return;
    } finally {
      if (renderTask === task) renderTask = null;
    }
    if (revision !== renderRevision) return;

    const content = await currentPage.getTextContent();
    if (revision !== renderRevision) return;
    textItems = content.items
      .filter((item) => item.str && item.str.trim())
      .map((item) => {
        const size = Math.hypot(item.transform[2], item.transform[3]) * baseScale * zoom;
        const point = viewport.convertToViewportPoint(item.transform[4], item.transform[5]);
        return {
          text: item.str,
          x: point[0],
          y: point[1] - size,
          w: Math.max(1, item.width * baseScale * zoom),
          h: Math.max(1, size),
        };
      });

    renderMarks();
    $("#pageCount").textContent = `${page} / ${pdf.numPages}`;
    $("#prevBtn").disabled = page <= 1;
    $("#nextBtn").disabled = page >= pdf.numPages;
    $("#zoomVal").textContent = `${Math.round(zoom * 100)}%`;
  } catch (error) {
    if (revision === renderRevision) toast("Не удалось отобразить страницу PDF");
    console.error(error);
  }
}

async function openFile(file) {
  if (!file) return;
  if (file.size > MAX_PDF_BYTES) {
    toast("Файл больше 100 МБ");
    return;
  }
  const revision = ++loadRevision;
  const openButton = $("#openBtn");
  openButton.disabled = true;
  openButton.textContent = "Открываю…";

  try {
    const bytes = await file.arrayBuffer();
    const loadedPdf = await pdfjsLib.getDocument({
      data: new Uint8Array(bytes.slice(0)),
    }).promise;
    if (revision !== loadRevision) {
      await loadedPdf.destroy();
      return;
    }

    const oldPdf = pdf;
    renderRevision += 1;
    renderTask?.cancel();
    renderTask = null;
    pdf = loadedPdf;
    sourceBytes = bytes;
    fileName = file.name;
    page = 1;
    zoom = 1;
    pending = null;
    replacementCount = 0;
    $("#filename").textContent = fileName;
    $("#saveBtn").disabled = false;
    $("#welcome")?.remove();
    await oldPdf?.destroy();
    await render();
    toast(`Открыто страниц: ${pdf.numPages}`);
  } catch (error) {
    if (revision === loadRevision) {
      const message = error.name === "PasswordException"
        ? "PDF защищён паролем"
        : "Не удалось открыть PDF. Проверьте файл.";
      toast(message);
      console.error(error);
    }
  } finally {
    if (revision === loadRevision) {
      openButton.disabled = false;
      openButton.textContent = "Открыть PDF";
    }
  }
}

function showReplaceDialog(item, rect) {
  pending = {
    text: item.text,
    x: item.x / rect.width,
    y: item.y / rect.height,
    w: item.w / rect.width,
    h: item.h / rect.height,
  };
  $("#oldText").textContent = item.text;
  $("#newText").value = "";
  previousFocus = document.activeElement;
  modal.classList.add("show");
  $("#newText").focus();
  renderMarks();
}

function closeReplaceDialog() {
  if ($("#confirmReplace").disabled) return;
  modal.classList.remove("show");
  pending = null;
  renderMarks();
  previousFocus?.focus();
}

async function replaceText(value) {
  if (!pending || !sourceBytes) return;
  const button = $("#confirmReplace");
  const cancelButton = $("#cancelReplace");
  const openButton = $("#openBtn");
  button.disabled = true;
  cancelButton.disabled = true;
  openButton.disabled = true;
  button.textContent = "Заменяю…";
  try {
    const response = await fetch("/api/replace", {
      method: "POST",
      headers: {
        "Content-Type": "application/pdf",
        "X-Pdf-Page": String(page - 1),
        "X-Pdf-Box": JSON.stringify({
          x: pending.x,
          y: pending.y,
          w: pending.w,
          h: pending.h,
        }),
        "X-Replace-Text": encodeURIComponent(value),
      },
      body: sourceBytes,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({ error: "Не удалось заменить текст" }));
      throw new Error(error.error || "Не удалось заменить текст");
    }

    const changedBytes = await response.arrayBuffer();
    const changedPdf = await pdfjsLib.getDocument({
      data: new Uint8Array(changedBytes.slice(0)),
    }).promise;
    const oldPdf = pdf;
    sourceBytes = changedBytes;
    pdf = changedPdf;
    pending = null;
    replacementCount += 1;
    modal.classList.remove("show");
    await oldPdf?.destroy();
    await render();
    toast("Текст заменён и сохранён в PDF");
    previousFocus?.focus();
  } catch (error) {
    toast(error.message || "Не удалось заменить текст");
    console.error(error);
  } finally {
    button.disabled = false;
    cancelButton.disabled = false;
    openButton.disabled = false;
    button.textContent = "Заменить";
  }
}

function downloadPdf() {
  if (!sourceBytes) return;
  const blob = new Blob([sourceBytes], { type: "application/pdf" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${fileName.replace(/\.pdf$/i, "")}-изменённый.pdf`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 3000);
  toast(replacementCount
    ? `Сохранён PDF с заменами: ${replacementCount}`
    : "Сохранён PDF");
}

$("#openBtn").addEventListener("click", () => input.click());
$("#welcomeOpen").addEventListener("click", () => input.click());
$("#replaceTool").addEventListener("click", () => {
  toast(pdf ? "Нажмите на текст в документе" : "Сначала откройте PDF-файл");
});
input.addEventListener("change", () => {
  void openFile(input.files[0]);
  input.value = "";
});
$("#saveBtn").addEventListener("click", downloadPdf);
$("#prevBtn").addEventListener("click", () => {
  if (page > 1) {
    page -= 1;
    pending = null;
    void render();
  }
});
$("#nextBtn").addEventListener("click", () => {
  if (pdf && page < pdf.numPages) {
    page += 1;
    pending = null;
    void render();
  }
});
$("#zoomIn").addEventListener("click", () => {
  zoom = Math.min(zoom * 1.15, 2.5);
  void render();
});
$("#zoomOut").addEventListener("click", () => {
  zoom = Math.max(zoom / 1.15, 0.55);
  void render();
});

stage.addEventListener("pointerdown", (event) => {
  const layer = event.target.closest(".layer");
  if (!layer || modal.classList.contains("show")) return;
  const rect = layer.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const hit = textItems
    .map((item) => ({
      item,
      distance: Math.hypot(
        Math.max(item.x - x, 0, x - (item.x + item.w)),
        Math.max(item.y - y, 0, y - (item.y + item.h)),
      ),
    }))
    .filter(({ distance }) => distance < Math.max(12, baseScale * zoom * 7))
    .sort((a, b) => a.distance - b.distance)[0]?.item;

  if (!hit) {
    toast("Нажмите на текст, который можно выделить");
    return;
  }
  showReplaceDialog(hit, rect);
});

$("#cancelReplace").addEventListener("click", closeReplaceDialog);
modal.addEventListener("pointerdown", (event) => {
  if (event.target === modal) closeReplaceDialog();
});
$("#confirmReplace").addEventListener("click", () => {
  const value = $("#newText").value.trim();
  if (value) void replaceText(value);
  else toast("Введите новый текст");
});
$("#newText").addEventListener("keydown", (event) => {
  if (event.key === "Enter") $("#confirmReplace").click();
});
modal.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    closeReplaceDialog();
  }
  if (event.key !== "Tab") return;
  const controls = [...modal.querySelectorAll("input, button:not(:disabled)")];
  const first = controls[0];
  const last = controls[controls.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

for (const eventName of ["dragenter", "dragover"]) {
  stage.addEventListener(eventName, (event) => {
    event.preventDefault();
    stage.classList.add("dragging");
  });
}
for (const eventName of ["dragleave", "drop"]) {
  stage.addEventListener(eventName, (event) => {
    event.preventDefault();
    stage.classList.remove("dragging");
  });
}
stage.addEventListener("drop", (event) => {
  const file = [...(event.dataTransfer?.files || [])]
    .find((item) => item.type === "application/pdf" || item.name.toLowerCase().endsWith(".pdf"));
  if (file) void openFile(file);
  else toast("Перетащите сюда PDF-файл");
});

window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => void render(), 100);
});
