// ─── State ───────────────────────────────────────────────────────────────────
let slides = [];
let current = 0;
let animationQueue = [];
let animationStep = 0;
let isAnimating = false;

let lessonId = new URLSearchParams(window.location.search).get("lesson");
let curriculum = null;
let progressSaveTimer = null;
let activeConceptId = null;
let lastScene = "slideshow";

let slideAnimating = false;

// auth
let authMode = "login";

// info card
let infoPanelOpen = false;
let activeInfoElement = null;
let selectedConcept = null;
let pronounceAudio = null;          // separate from currentAudio — never touches narration playback
const pronounceCache = new Map();   // term text → blob URL
const PRONOUNCE_CACHE_LIMIT = 10;

// tts
let currentSpeed = 1;
let currentVolume = 1;
let currentAudio = null;
let audioPaused = false;
const ttsCache = new Map();      // notes text → blob URL, cached for the session
const TTS_CACHE_LIMIT = 5;
let ttsRequestId = 0;
let sentenceBoundaries = [];     // [{start, end}] in native audio seconds — speed independent
let currentSentenceIndex = -1;
let highlightRAF = null;
let autoPlayTimer = null;

// ─── Auth ─────────────────────────────────────────────────────────────────────
let currentUser = null; // { email } or null

async function checkAuth() {
  try {
    const res = await fetch("/api/auth/me");
    const data = await res.json();
    currentUser = data.logged_in ? { email: data.email } : null;
  } catch {
    currentUser = null;
  }
  renderAuthButton();
}

function renderAuthButton() {
  document.querySelectorAll(".auth-control").forEach(el => {
    el.innerHTML = "";
    const btn = document.createElement("button");
    btn.className = "auth-btn";
    btn.textContent = currentUser ? currentUser.email : "Log in";
    el.appendChild(btn);

    if (!currentUser) {
      btn.onclick = () => switchScene("auth");
      return;
    }

    const menu = document.createElement("div");
    menu.className = "auth-dropdown";
    menu.innerHTML = `
      <button class="auth-dropdown-item" data-action="account">My Account</button>
      <button class="auth-dropdown-item" data-action="logout">Log out</button>`;
    el.appendChild(menu);

    btn.onclick = (e) => {
      e.stopPropagation();
      menu.classList.toggle("open");
    };
    menu.querySelector('[data-action="account"]').onclick = (e) => {
      e.stopPropagation();
      menu.classList.remove("open");
      switchScene("account");
    };
    menu.querySelector('[data-action="logout"]').onclick = (e) => {
      e.stopPropagation();
      menu.classList.remove("open");
      logout();
    };
  });
}

// close any open auth dropdown on outside click — same pattern as tts-speed-menu
document.addEventListener("click", () => {
  document.querySelectorAll(".auth-dropdown.open").forEach(m => m.classList.remove("open"));
});

const eyeOpen = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
const eyeClosed = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.94 10.94 0 0112 20c-7 0-11-8-11-8a21.8 21.8 0 015.06-6.06M9.9 4.24A10.94 10.94 0 0112 4c7 0 11 8 11 8a21.8 21.8 0 01-2.16 3.19M1 1l22 22"/></svg>`;

// shared by the login/signup form and the change-password form
function initPasswordToggle(toggleEl, inputEl) {
  toggleEl.innerHTML = eyeClosed;
  toggleEl.addEventListener("click", () => {
    const show = inputEl.type === "password";
    inputEl.type = show ? "text" : "password";
    toggleEl.innerHTML = show ? eyeOpen : eyeClosed;
  });
}

function resetPasswordVisibility(inputEl, toggleEl) {
  inputEl.type = "password";
  toggleEl.innerHTML = eyeClosed;
}

function initAuthForm() {
  const form = document.getElementById("auth-form");
  const toggle = document.getElementById("auth-toggle-mode");
  const errorEl = document.getElementById("auth-error");

  document.getElementById("auth-title").textContent = authMode === "login" ? "Welcome back" : "Create an account";

  initPasswordToggle(document.getElementById("auth-password-toggle"), document.getElementById("auth-password"));

  toggle.onclick = () => {
    authMode = authMode === "login" ? "signup" : "login";
    document.getElementById("auth-submit").textContent = authMode === "login" ? "Log in" : "Sign up";
    document.getElementById("auth-title").textContent = authMode === "login" ? "Welcome back" : "Create an account";
    document.getElementById("auth-subtitle").textContent = authMode === "login" ? "Log in to continue" : "Sign up to get started";
    toggle.textContent = authMode === "login" ? "Need an account? Sign up" : "Have an account? Log in";
    errorEl.textContent = "";
    form.reset();
    resetPasswordVisibility(document.getElementById("auth-password"), document.getElementById("auth-password-toggle"));
  };

  form.onsubmit = async (e) => {
    e.preventDefault();
    errorEl.textContent = "";
    const email = document.getElementById("auth-email").value.trim();
    const password = document.getElementById("auth-password").value;

    try {
      const res = await fetch(`/api/auth/${authMode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Something went wrong");

      currentUser = { email: data.email };
      renderAuthButton();
      form.reset();
      resetPasswordVisibility(document.getElementById("auth-password"), document.getElementById("auth-password-toggle"));
      loadLessonLibrary(); // refresh library, claimed lessons may now show
      switchScene(lastScene === "auth" ? "welcome" : lastScene);
    } catch (err) {
      errorEl.textContent = err.message;
    }
  };
}

async function loadAccountPage() {
  const el = document.getElementById("account-content");
  el.innerHTML = "<p>Loading...</p>";
  try {
    const res = await fetch("/api/auth/me");
    const data = await res.json();
    if (!data.logged_in) { el.innerHTML = "<p>Not logged in.</p>"; return; }

    const joined = new Date(data.member_since).toLocaleDateString();
    el.innerHTML = `
      <div class="account-card">
        <div class="account-avatar"></div>
        <p class="account-label">Email</p>
        <p class="account-email"></p>
        <p class="account-since">Member since ${joined}</p>
        <div class="account-stats"> ... </div>
        <button id="account-logout-btn" class="card-btn">Log out</button>
        <button id="account-change-pw-btn" class="card-btn">Change password</button>
        <form id="change-pw-form" class="hidden">
          <div class="auth-password-wrap">
            <input type="password" id="current-password" placeholder="Current password" required />
            <button type="button" class="auth-password-toggle-btn" id="current-password-toggle" aria-label="Show password"></button>
          </div>
          <div class="auth-password-wrap">
            <input type="password" id="new-password" placeholder="New password" required />
            <button type="button" class="auth-password-toggle-btn" id="new-password-toggle" aria-label="Show password"></button>
          </div>
          <p id="change-pw-error" class="auth-error"></p>
          <button type="submit" class="card-btn" disabled>Save</button>
        </form>
        <button id="account-delete-btn" class="card-btn card-btn-danger">Delete account</button>
      </div>`;
    el.querySelector(".account-avatar").textContent = data.email.charAt(0).toUpperCase();
    el.querySelector(".account-email").textContent = data.email;
    document.getElementById("account-logout-btn").onclick = logout;
    document.getElementById("account-delete-btn").onclick = confirmDeleteAccount;
    document.getElementById("account-change-pw-btn").onclick = () => {
      document.getElementById("change-pw-form").classList.toggle("hidden");
    };
    const currentPwInput = document.getElementById("current-password");
    const newPwInput = document.getElementById("new-password");
    initPasswordToggle(document.getElementById("current-password-toggle"), currentPwInput);
    initPasswordToggle(document.getElementById("new-password-toggle"), newPwInput);
    const saveBtn = document.querySelector("#change-pw-form button[type=submit]");
    const updateSaveState = () => {
      saveBtn.disabled = !currentPwInput.value || !newPwInput.value;
    };
    currentPwInput.addEventListener("input", updateSaveState);
    newPwInput.addEventListener("input", updateSaveState);
    document.getElementById("change-pw-form").onsubmit = async (e) => {
      e.preventDefault();
      const errorEl = document.getElementById("change-pw-error");
      errorEl.textContent = "";
      try {
        const res = await fetch("/api/auth/change-password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            current_password: document.getElementById("current-password").value,
            new_password: document.getElementById("new-password").value
          })
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "Could not change password");
        loadAccountPage(); // refresh, clears the form
      } catch (err) {
        errorEl.textContent = err.message;
      }
    };
  } catch (err) {
    el.innerHTML = `<p>Could not load account info.</p>`;
  }
}

async function logout() {
  await fetch("/api/auth/logout", { method: "POST" }).catch(() => {});
  currentUser = null;
  stopNarrationFully();
  document.getElementById("auth-form")?.reset();
  const pwInput = document.getElementById("auth-password");
  const pwToggle = document.getElementById("auth-password-toggle");
  if (pwInput && pwToggle) resetPasswordVisibility(pwInput, pwToggle);
  renderAuthButton();
  showWelcome();
}

// ─── Boot ─────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  if (lessonId) {
    enterLesson(lessonId);
  } else {
    showWelcome();
  }
  document.addEventListener("keydown", handleKeyboard);
  initUpload();
  checkAuth();
  initAuthForm();
});

function enterLesson(id) {
  lessonId = id;
  const scene = new URLSearchParams(window.location.search).get("scene"); // read before URL gets overwritten
  history.replaceState(null, "", scene ? `?lesson=${id}&scene=${scene}` : `?lesson=${id}`); // preserve it
  showScreen("loading");
  pollLessonStatus(id)
    .then(() => loadSlideshow(false))   // resuming, not a fresh finish
    .then(() => {
      if (scene === "study") switchScene("study"); // use the captured value, not a fresh (now-empty) lookup
    })
}

async function loadSlideshow(justGenerated = false) {
  try {
    const res = await fetch(`/api/lessons/${lessonId}/slideshow`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    slides = data.slideshow.slides;
    document.getElementById("course-name").textContent = data.slideshow.course;

    buildDotNav();
    const progRes = await fetch(`/api/lessons/${lessonId}/progress`); //check for saved position
    const prog = progRes.ok ? await progRes.json() : {};
    const startSlide = prog.last_viewed_slide != null ? prog.last_viewed_slide : 0; 

    document.getElementById("tts-mute-btn").onclick = () => {
      const muteBtn = document.getElementById("tts-mute-btn");
      const muted = muteBtn.classList.toggle("tts-btn-active");
      muteBtn.textContent = muted ? "🔇" : "🔊";
      if (currentAudio) currentAudio.muted = muted;

      document.getElementById("tts-volume-slider").value = muted ? 0 : currentVolume;  // ← add this
    };

    const volumeSlider = document.getElementById("tts-volume-slider");
    volumeSlider.oninput = (e) => {
      currentVolume = parseFloat(e.target.value);
      if (currentAudio) currentAudio.volume = currentVolume;

      const muteBtn = document.getElementById("tts-mute-btn");
      if (muteBtn.classList.contains("tts-btn-active")) { 
        muteBtn.classList.remove("tts-btn-active");
        muteBtn.textContent = "🔊";
        if (currentAudio) currentAudio.muted = false;
      }
    };

    document.getElementById("tts-skip-start-btn").onclick = skipToStart; 
    document.getElementById("tts-skip-end-btn").onclick = skipToEnd; 

    const speedBtn = document.getElementById("tts-speed-btn");
    const speedMenu = document.getElementById("tts-speed-menu");

    speedBtn.onclick = (e) => {
      e.stopPropagation();
      speedMenu.classList.toggle("open");
    };

    speedMenu.querySelectorAll(".tts-speed-item").forEach(item => {
      item.onclick = (e) => {
        e.stopPropagation();
        currentSpeed = parseFloat(item.dataset.speed);

        // update active state
        speedMenu.querySelectorAll(".tts-speed-item").forEach(i => i.classList.remove("speed-active"));
        item.classList.add("speed-active");

        // update button label
        speedBtn.textContent = currentSpeed === 1 ? "1×" : `${currentSpeed}×`;

        // apply to current audio
        if (currentAudio) {
          currentAudio.playbackRate = currentSpeed;
        }

        speedMenu.classList.remove("open");
      };
    });

    // close on outside click
    document.addEventListener("click", () => speedMenu.classList.remove("open"));

    loadCurriculum(); // prefetch, independent of which screen is visible

    const scene = new URLSearchParams(window.location.search).get("scene");

    const enterSlideshow = () => {
      renderSlide(startSlide);       // autoplay fires here now, only after a real click
      showScreen("slideshow");
      lastScene = "slideshow";
      fitSlideToStage();
      window.addEventListener("resize", fitSlideToStage);
    };

    if (justGenerated && scene !== "study") {
      document.getElementById("start-lesson-title").textContent = data.slideshow.course;
      showScreen("start");
      document.getElementById("lesson-start-btn").onclick = enterSlideshow;
    } else {
      enterSlideshow();
    }
  } catch (err) {
    if (err.message === "HTTP 404") {
      showScreen("welcome");
      lastScene = "welcome";
      history.replaceState(null, "", window.location.pathname);
    } else {
      document.getElementById("error-message").textContent = err.message;
      showScreen("error");
    }
  }
}



// ─── Welcome screen ───────────────────────────────────────────────────────────
function showWelcome() {
  lastScene = "welcome"; // so Back-from-Progress can return here
  document.getElementById("welcome-screen").classList.remove("hidden");
  document.getElementById("loading-screen").classList.add("hidden");
  document.getElementById("slideshow").classList.add("hidden");
  document.getElementById("study").classList.add("hidden");
}

async function loadLessonLibrary() {
  const section = document.getElementById("lesson-library");
  const list = document.getElementById("lesson-library-list");
  try {
    const res = await fetch("/api/lessons");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const lessons = await res.json();

    list.innerHTML = "";
    lessons.forEach(meta => {
      const item = document.createElement("button");
      item.className = "lesson-library-item";
      item.disabled = meta.status !== "ready";
      const fullName = meta.course || meta.source_filename || meta.lesson_id;
      const colonIdx = fullName.indexOf(":");

      const nameSpan = document.createElement("span");
      nameSpan.className = "lesson-library-name";
      if (colonIdx === -1) {
        const titleSpan = document.createElement("span");
        titleSpan.className = "lesson-library-title";
        titleSpan.textContent = fullName;
        nameSpan.appendChild(titleSpan);
      } else {
        const titleSpan = document.createElement("span");
        titleSpan.className = "lesson-library-title";
        titleSpan.textContent = fullName.slice(0, colonIdx);
        const subtitleSpan = document.createElement("span");
        subtitleSpan.className = "lesson-library-subtitle";
        subtitleSpan.textContent = fullName.slice(colonIdx + 1).trim();
        nameSpan.append(titleSpan, subtitleSpan);
      }

      const statusSpan = document.createElement("span");
      statusSpan.className = `lesson-library-status lesson-status-${meta.status}`;
      statusSpan.textContent = meta.status;

      const deleteBtn = document.createElement("button");
      deleteBtn.className = "lesson-library-delete";
      deleteBtn.setAttribute("aria-label", "Delete lesson");
      deleteBtn.textContent = "✕";

      item.append(nameSpan, statusSpan, deleteBtn);
      item.onclick = () => enterLesson(meta.lesson_id);
      item.querySelector(".lesson-library-delete").onclick = (e) => {
        e.stopPropagation(); // don't trigger enterLesson
        confirmDeleteLesson(meta.lesson_id);
      };
      list.appendChild(item);

      if (meta.status === "ready") {
        fetch(`/api/lessons/${meta.lesson_id}/progress`)
          .then(res => res.json())
          .then(progress => {
            const statusEl = item.querySelector(".lesson-library-status");
            if (progress.completed) {
              statusEl.textContent = "Completed";
            } else if (progress.last_viewed_slide != null && meta.slide_count) {
              statusEl.textContent = `${progress.last_viewed_slide + 1}/${meta.slide_count} · Resume`;
            }
          })
          .catch(() => {});
      }
    });

    section.classList.toggle("hidden", lessons.length === 0);
  } catch (err) {
    console.warn("Could not load lesson library:", err.message);
    section.classList.add("hidden");
  }
}

// ─── Delete modal (shared by lessons and account deletion) ──────────────────

let pendingDeleteAction = null; // { type: "lesson", id } or { type: "account" }

function confirmDeleteLesson(id) {
  pendingDeleteAction = { type: "lesson", id };
  document.getElementById("delete-modal-message").textContent = "Deleting lesson. Are you sure?";
  document.getElementById("delete-modal").classList.remove("hidden");
}

function confirmDeleteAccount() {
  pendingDeleteAction = { type: "account" };
  document.getElementById("delete-modal-message").textContent =
    "This will permanently delete your account and all associated lessons and progress. Are you sure?";
  document.getElementById("delete-modal").classList.remove("hidden");
}

function cancelDeleteModal() {
  pendingDeleteAction = null;
  document.getElementById("delete-modal").classList.add("hidden");
}

async function confirmDeleteModal() {
  document.getElementById("delete-modal").classList.add("hidden");
  const action = pendingDeleteAction;
  pendingDeleteAction = null;
  if (!action) return;

  if (action.type === "lesson") {
    try {
      const res = await fetch(`/api/lessons/${action.id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      loadLessonLibrary();
    } catch (err) {
      console.warn("Could not delete lesson:", err.message);
    }
  } else if (action.type === "account") {
    try {
      const res = await fetch("/api/auth/delete-account", { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      currentUser = null;
      renderAuthButton();
      showWelcome();
    } catch (err) {
      console.warn("Could not delete account:", err.message);
    }
  }
}

function initUpload() {
  const input    = document.getElementById("file-input");
  const area     = document.getElementById("upload-area");

  // click anywhere on the area triggers the file input
  area.addEventListener("click", () => input.click());

  input.addEventListener("change", () => {
    if (input.files[0]) setFile(input.files[0]);
  });

  // drag and drop
  area.addEventListener("dragover", e => {
    e.preventDefault();
    area.classList.add("drag-over");
  });
  area.addEventListener("dragleave", () => area.classList.remove("drag-over"));
  area.addEventListener("drop", e => {
    e.preventDefault();
    area.classList.remove("drag-over");
    const file = e.dataTransfer.files[0];
    if (file) setFile(file);
  });
}

function setFile(file) {
  const allowed = ["application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"];
  const allowedExt = [".pdf", ".docx", ".pptx"];
  const ext = "." + file.name.split(".").pop().toLowerCase();

  if (!allowedExt.includes(ext)) {
    alert("Please upload a PDF, DOCX, or PPTX file.");
    return;
  }

  document.getElementById("file-name").textContent = file.name;
  document.getElementById("file-status").classList.remove("hidden");
  document.getElementById("upload-area").classList.add("has-file");
  document.getElementById("btn-generate").disabled = false;
}

function clearFile() {
  document.getElementById("file-input").value = "";
  document.getElementById("file-name").textContent = "";
  document.getElementById("file-status").classList.add("hidden");
  document.getElementById("upload-area").classList.remove("has-file");
  document.getElementById("btn-generate").disabled = true;
}

async function generateLesson() {
  document.getElementById("welcome-screen").classList.add("hidden");
  showScreen("loading");

  try {
    const file = document.getElementById("file-input").files[0];
    const form = new FormData();
    form.append("file", file);
    clearFile();

    const uploadRes = await fetch("/api/lessons", { method: "POST", body: form });
    const data = await uploadRes.json();
    if (!uploadRes.ok) throw new Error(data.error || "Upload failed");
    lessonId = data.lesson_id;

    history.replaceState(null, "", `?lesson=${lessonId}`);

    await pollLessonStatus(lessonId);
    await loadSlideshow(true);
  } catch (err) {
    document.getElementById("error-message").textContent = err.message;
    document.querySelector(".error-code").textContent = err.message.match(/\d{3}/)?.[0] || "!";
    showScreen("error");
  }
}

async function pollLessonStatus(id) {
  let consecutiveErrors = 0;
  while (true) {
    let res;
    try {
      res = await fetch(`/api/lessons/${id}/status`);
    } catch {
      res = null; // network hiccup — treat like a failed attempt below
    }

    if (res && res.ok) {
      consecutiveErrors = 0;
      const meta = await res.json();
      if (meta.status === "ready") return;
      if (meta.status === "failed") throw new Error(meta.error || "Lesson generation failed");
      document.getElementById("loader-message").textContent = meta.label || "Loading lesson…";
      if (meta.progress != null) {
        document.getElementById("loader-bar").style.width = `${meta.progress * 100}%`;
      }
    } else {
      consecutiveErrors++;
      if (consecutiveErrors >= 5) {
        throw new Error(res ? `HTTP ${res.status}` : "Network error");
      }
    }

    await new Promise(r => setTimeout(r, 1500));
  }
}

async function retryFailedLesson() {
  if (!lessonId) { doGoHome(); return; }
  showScreen("loading");
  document.getElementById("loader-message").textContent = "Retrying…";
  document.getElementById("loader-bar").style.width = "0%";

  try {
    const res = await fetch(`/api/lessons/${lessonId}/retry`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Retry failed");

    await pollLessonStatus(lessonId);
    await loadSlideshow(true);
  } catch (err) {
    document.getElementById("error-message").textContent = err.message;
    document.querySelector(".error-code").textContent = err.message.match(/\d{3}/)?.[0] || "!";
    showScreen("error");
  }
}

// ─── Back to Home ───────────────────────────────────────────────────────────

function confirmHome() {
  if (!lessonId) { doGoHome(); return; }
  document.getElementById("home-modal").classList.remove("hidden");
}

function cancelHome() {
  document.getElementById("home-modal").classList.add("hidden");
}

function doGoHome() {
  document.getElementById("home-modal").classList.add("hidden");
  stopNarrationFully();
  lessonId = null;
  history.replaceState(null, "", window.location.pathname);
  showWelcome();
}


// ─── Student Progress Section  ────────────────────────────────────────────────────────────

async function loadProgressPage() {
  const el = document.getElementById("progress-content");
  el.innerHTML = `<h1 class="lessons-page-heading">My Progress</h1>` + "<p>Loading…</p>";
  const res = await fetch("/api/progress");
  const data = await res.json();

  el.innerHTML = `<h1 class="lessons-page-heading">My Progress</h1>`;;
  data.forEach(({ lesson, progress, quiz_history }) => {
    const byConceptScore = {};
    quiz_history.forEach(h => {
      byConceptScore[h.concept_id] ??= { name: h.concept_name, attempts: [] };
      byConceptScore[h.concept_id].attempts.push(h);
    });

    const card = document.createElement("div");
    card.className = "progress-lesson-card";
    const pct = progress.last_viewed_slide != null && lesson.slide_count
      ? `Progress: ${progress.last_viewed_slide + 1}/${lesson.slide_count} slides completed` : "Not started";

    // average of each concept's latest run (concepts with no attempts are excluded)
    const latestScores = Object.values(byConceptScore).map(c => {
      const runs = {};
      c.attempts.forEach(a => {
        runs[a.submitted_at] ??= { correct: 0, total: 0, at: a.submitted_at };
        runs[a.submitted_at].total++;
        if (a.is_correct) runs[a.submitted_at].correct++;
      });
      const runList = Object.values(runs).sort((a, b) => a.at.localeCompare(b.at));
      const latest = runList[runList.length - 1];
      return latest.correct / latest.total;
    });
    const avgScore = latestScores.length
      ? `Average quiz score: ${Math.round((latestScores.reduce((a, b) => a + b, 0) / latestScores.length) * 100)}%`
      : "Average quiz score: No attempts yet";

    const conceptsWithScores = latestScores.length;
    const showToggle = conceptsWithScores > 1;
    card.innerHTML = `
      ${showToggle ? `<button class="progress-card-toggle" aria-label="Collapse lesson">▾</button>` : ""}
      <h3>${lesson.course || lesson.source_filename}</h3>
      <p class="progress-slides">${progress.completed ? "Completed" : pct}</p>
      <p class="progress-avg-score">${avgScore}</p>
      <div class="progress-card-body">
        ${Object.values(byConceptScore).map(renderConceptProgress).join("")}
      </div>`;
    const toggleBtn = card.querySelector(".progress-card-toggle");
    if (toggleBtn) {
      toggleBtn.onclick = () => card.classList.toggle("collapsed");
    }
    el.appendChild(card);
  });
}

function renderConceptProgress(concept) {
  // group attempts by submitted_at (batch = one quiz run), score each run
  const runs = {};
  concept.attempts.forEach(a => {
    runs[a.submitted_at] ??= { correct: 0, total: 0, at: a.submitted_at };
    runs[a.submitted_at].total++;
    if (a.is_correct) runs[a.submitted_at].correct++;
  });
  const runList = Object.values(runs).sort((a, b) => a.at.localeCompare(b.at));
  const latest = runList[runList.length - 1];
  const best = runList.reduce((b, r) => (r.correct / r.total > b.correct / b.total ? r : b), runList[0]);

  const fmt = r => `${r.correct}/${r.total} (${Math.round((r.correct / r.total) * 100)}%)`;

  return `
    <div class="progress-concept-row">
      <div class="progress-concept-name">${concept.name}</div>
      <div class="progress-concept-summary">Latest: ${fmt(latest)} · Best: ${fmt(best)}</div>
      ${runList.length > 1 ? `
        <details class="progress-history-dropdown">
          <summary>View all ${runList.length} attempts</summary>
          ${runList.map((r, i) => `<div class="progress-history-row">Attempt ${i + 1}: ${fmt(r)}</div>`).join("")}
        </details>` : ""}
    </div>`;
}

// ─── Screen manager ───────────────────────────────────────────────────────────
function showScreen(name) {
  ["welcome-screen", "loading-screen", "error-screen", "start-screen", "slideshow"].forEach(id => {
    document.getElementById(id).classList.toggle("hidden", id !== `${name === "slideshow" ? "slideshow" : name + "-screen"}`);
  });

  if (name === "slideshow") {
    document.getElementById("slideshow").classList.remove("hidden");
    document.getElementById("loading-screen").classList.add("hidden");
    document.getElementById("error-screen").classList.add("hidden");
  }
}

// ─── Slide rendering ──────────────────────────────────────────────────────────
function renderSlide(index) {
  if (currentAudio) {
  currentAudio.pause();
  currentAudio = null;
  audioPaused = false;
  }

  current = index;
  const slide = slides[index];
  const container = document.getElementById("slide-container");

  // exit animation on old slide
  const old = container.querySelector(".slide.active");
  if (old) {
    old.classList.add("slide-exit");
    setTimeout(() => old.remove(), 350);
  }

  const el = buildSlideElement(slide);
  container.appendChild(el);

  // enter animation
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
        el.classList.add("active");
      
        fitSlideTitle(el);
        fitSlideContent(el);
    });
  });

  // update chrome
  updateChrome(index);

  // set up animation queue for this slide
  animationQueue = slide.animation_hints || [];
  animationStep = 0;

  // apply "with_previous" and "after_previous" auto-animations
  slideAnimating = true;
  scheduleAutoAnimations(el);

  // update notes panel content for this slide
  updateNotesPanel(slide);

  // cancel any pending auto-play from a previous slide
  if (autoPlayTimer) {
    clearTimeout(autoPlayTimer);
    autoPlayTimer = null;
  }

  if (slide.speaker_notes) {
    const notes = slide.speaker_notes;
    const snapId = ttsRequestId;   // snapshot before any async gap

    if (cacheGet(notes)) {
      // Cached — short delay then autoplay
      autoPlayTimer = setTimeout(() => {
        autoPlayTimer = null;
        const btn = document.getElementById("tts-narrate-btn");
        if (btn && !currentAudio && !audioPaused) {
          speakNotes(notes, btn);
        } else if (audioPaused) {
          // User cancelled during the 2s wait — leave as paused
          btn.textContent = "▶";
          btn.classList.remove("tts-btn-active");
          btn.onclick = () => speakNotes(notes, btn);
        }
      }, 2000);
    } else {
      prefetchTTS(notes).then(() => {
        if (ttsRequestId !== snapId) return; // navigated away or user cancelled
        if (audioPaused) return;             // user cancelled during fetch
        const btn = document.getElementById("tts-narrate-btn");
        if (btn && !currentAudio) speakNotes(notes, btn);
      });
    }
  }

  if (activeInfoElement) {
    activeInfoElement = null;
  }

  // render equations via KaTeX
  if (window.renderMathInElement) {
    // Only render math in non-example elements
    // Examples are handled by buildExampleNodes which calls katex.render() directly
    const mathTargets = el.querySelectorAll(
      ".slide-title, .body-text, .body-bullet, .body-definition, .body-equation, .highlight-hero-box"
    );
    mathTargets.forEach(target => {
      renderMathInElement(target, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "$",  right: "$",  display: false },
          { left: "\\(", right: "\\)", display: false },
          { left: "\\[", right: "\\]", display: true },
        ],
        throwOnError: false,
      });
    });
  }
}

function buildSlideElement(slide) {
  const el = document.createElement("div");
  el.className = `slide slide-type-${slide.type}`;
  el.dataset.slideId = slide.slide_id;

  // title
  const titleEl = document.createElement("h1");
  titleEl.className = "slide-title";
  titleEl.textContent = slide.title;
  el.appendChild(titleEl);

  if (slide.type === "animation") {
    buildLayout_animation(el, slide);
    return el;
  }

  const layout = slide.layout || "default";
  const body   = slide.body || [];

  // dispatch to layout builder
  switch (layout) {
    case "two_col":       buildLayout_twoCol(el, body);        break;
    case "highlight_box": buildLayout_highlightBox(el, body);  break;
    case "equation_hero": buildLayout_equationHero(el, body);  break;
    case "cards":         buildLayout_cards(el, body);         break;
    default:              buildLayout_default(el, body);       break;
  }

  return el;
}

function buildLayout_animation(el, slide) {
  const wrapper = document.createElement("div");
  wrapper.className = "slide-body layout-animation";

  if (!slide.video_path) {
    const fallback = document.createElement("p");
    fallback.className = "animation-unavailable";
    fallback.textContent = "Animation unavailable for this slide.";
    wrapper.appendChild(fallback);
    el.appendChild(wrapper);
    return;
  }

  const videoWrap = document.createElement("div");
  videoWrap.className = "animation-video-wrap";

  const video = document.createElement("video");
  video.className = "animation-video";
  video.src = `/api/lessons/${lessonId}/media/${slide.video_path}`;
  video.muted = true;
  video.playsInline = true;
  video.loop = true;
  video.autoplay = true;

  const controls = document.createElement("div");
  controls.className = "animation-controls";

  const playBtn = document.createElement("button");
  playBtn.type = "button";
  playBtn.className = "animation-play-btn";
  playBtn.setAttribute("aria-label", "Pause");
  playBtn.textContent = "\u23F8"; // starts playing (autoplay), so show pause icon

  const seekBar = document.createElement("input");
  seekBar.type = "range";
  seekBar.className = "animation-seek";
  seekBar.min = "0";
  seekBar.max = "100";
  seekBar.value = "0";
  seekBar.step = "0.1";

  controls.append(playBtn, seekBar);

  playBtn.addEventListener("click", () => {
    video.paused ? video.play() : video.pause();
    playBtn.blur();
  });
  video.style.cursor = "pointer";
  video.addEventListener("click", () => {
    video.paused ? video.play() : video.pause();
  });
  video.addEventListener("play",  () => {
    playBtn.textContent = "\u23F8";
    playBtn.setAttribute("aria-label", "Pause");
    videoWrap.classList.remove("is-paused");
  });
  video.addEventListener("pause", () => {
    playBtn.textContent = "\u25B6";
    playBtn.setAttribute("aria-label", "Play");
    videoWrap.classList.add("is-paused");
  });

  // keep the bar in sync with playback, but not while the user is dragging it
  video.addEventListener("timeupdate", () => {
    if (!seekBar.matches(":active") && video.duration) {
      seekBar.value = (video.currentTime / video.duration) * 100;
    }
  });
  seekBar.addEventListener("input", () => {
    if (video.duration) video.currentTime = (seekBar.value / 100) * video.duration;
  });
  seekBar.addEventListener("change", () => {
    seekBar.blur();
  });

  videoWrap.append(video, controls);
  wrapper.appendChild(videoWrap);
  el.appendChild(wrapper);
}

// ── helpers shared by all layout builders ─────────────────────────────────────

function _appendBodyItem(container, item, globalIndex) {
  if (item.type === "col_break") return; // structural marker — not rendered
  const itemEl = buildBodyItem(item, globalIndex);
  itemEl.dataset.bodyIndex = globalIndex;
  itemEl.classList.add("anim-hidden");
  container.appendChild(itemEl);
}

// ── Layout: default (single column) ───────────────────────────────────────────
function buildLayout_default(el, body) {
  const bodyEl = document.createElement("div");
  bodyEl.className = "slide-body layout-default";
  body.forEach((item, i) => _appendBodyItem(bodyEl, item, i));
  el.appendChild(bodyEl);
}

// ── Layout: two_col ────────────────────────────────────────────────────────────
function buildLayout_twoCol(el, body) {
  const wrapper = document.createElement("div");
  wrapper.className = "slide-body layout-two-col";

  const left  = document.createElement("div");
  left.className  = "two-col-left";
  const right = document.createElement("div");
  right.className = "two-col-right";

  let inRight = false;
  body.forEach((item, i) => {
    if (item.type === "col_break") { inRight = true; return; }
    _appendBodyItem(inRight ? right : left, item, i);
  });

  // if no col_break was found, split evenly at midpoint
  if (!inRight) {
    const mid = Math.ceil(body.length / 2);
    left.innerHTML = "";
    right.innerHTML = "";
    body.forEach((item, i) => {
      _appendBodyItem(i < mid ? left : right, item, i);
    });
  }

  wrapper.appendChild(left);
  wrapper.appendChild(right);
  el.appendChild(wrapper);
}

// ── Layout: highlight_box ──────────────────────────────────────────────────────
function buildLayout_highlightBox(el, body) {
  const wrapper = document.createElement("div");
  wrapper.className = "slide-body layout-highlight-box";

  // first text element → hero callout block
  let heroUsed = false;
  const rest = [];

  body.forEach((item) => {
    if (!heroUsed && item.type === "text") {
      const box = document.createElement("div");
      box.className = "highlight-hero-box";
      box.textContent = item.content;
      wrapper.appendChild(box);
      heroUsed = true;
    } else {
      rest.push(item);
    }
  });

  // remaining items below
  if (rest.length) {
    const below = document.createElement("div");
    below.className = "highlight-below";
    rest.forEach((item, i) => _appendBodyItem(below, item, i));
    wrapper.appendChild(below);
  }

  el.appendChild(wrapper);
}

// ── Layout: equation_hero ──────────────────────────────────────────────────────
function buildLayout_equationHero(el, body) {
  const wrapper = document.createElement("div");
  wrapper.className = "slide-body layout-equation-hero";

  let heroUsed = false;
  const rest = [];

  body.forEach((item, i) => {
    if (!heroUsed && item.type === "equation") {
      const heroWrap = document.createElement("div");
      heroWrap.className = "eq-hero-wrap";
      const itemEl = buildBodyItem(item, i);
      itemEl.classList.add("anim-hidden", "eq-hero-item");
      itemEl.dataset.bodyIndex = i;
      heroWrap.appendChild(itemEl);
      wrapper.appendChild(heroWrap);
      heroUsed = true;
    } else {
      rest.push({ item, i });
    }
  });

  if (rest.length) {
    const below = document.createElement("div");
    below.className = "eq-hero-below";
    rest.forEach(({ item, i }) => _appendBodyItem(below, item, i));
    wrapper.appendChild(below);
  }

  el.appendChild(wrapper);
}

// ── Layout: cards ──────────────────────────────────────────────────────────────
function buildLayout_cards(el, body) {
  const wrapper = document.createElement("div");
  wrapper.className = "slide-body layout-cards";

  // text items before first def/example → intro prose
  const firstCardIdx = body.findIndex(
    item => item.type === "definition" || item.type === "example"
  );

  if (firstCardIdx > 0) {
    const intro = document.createElement("div");
    intro.className = "cards-intro";
    body.slice(0, firstCardIdx).forEach((item, i) => _appendBodyItem(intro, item, i));
    wrapper.appendChild(intro);
  }

  const grid = document.createElement("div");
  grid.className = "cards-grid";

  body.slice(firstCardIdx === -1 ? 0 : firstCardIdx).forEach((item, i) => {
    if (item.type === "definition" || item.type === "example") {
      const card = document.createElement("div");
      card.className = `card-tile card-tile-${item.type}`;
      const globalIdx = firstCardIdx + i;
      const itemEl = buildBodyItem(item, globalIdx);
      itemEl.classList.add("anim-hidden");
      itemEl.dataset.bodyIndex = globalIdx;
      card.appendChild(itemEl);
      grid.appendChild(card);
    }
  });

  wrapper.appendChild(grid);
  el.appendChild(wrapper);
}

// ── Build individual body item element ─────────────────────────────────────────
function buildBodyItem(item, index) {
  const el = document.createElement("div");
  el.className = `body-item body-${item.type}`;

  switch (item.type) {
    case "bullet": {
      el.classList.add(`bullet-level-${item.level ?? 0}`);
      const marker = document.createElement("span");
      marker.className = "bullet-marker";
      marker.textContent = item.level === 0 ? "●" : item.level === 1 ? "◦" : "▸";
      const text = document.createElement("span");
      text.className = "bullet-text";
      text.textContent = item.content;
      el.appendChild(marker);
      el.appendChild(text);
      break;
    }
    case "equation": {
      el.classList.add("equation-block", "clickable-info");
      // Normalize double-escaped backslashes from LLM output
      const latex = item.content.replace(/\\\\/g, "\\");
      el.textContent = `$$${latex}$$`;
      el.addEventListener("click", (e) => {
        if (slideAnimating) return;
        e.stopPropagation();
        openInfoPanel({ panelType: "equation", conceptId: item.concept_id || null, title: latex, content: latex }, el);
      });
      break;
    }
    case "definition": {
      el.classList.add("clickable-info");
      const label = document.createElement("span");
      label.className = "def-label";
      label.textContent = "DEF";
      const text = document.createElement("span");
      text.className = "def-text";
      text.textContent = item.content;
      el.appendChild(label);
      el.appendChild(text);
      el.addEventListener("click", (e) => {
        if (slideAnimating) return;
        e.stopPropagation();
        openInfoPanel({ panelType: "definition", conceptId: item.concept_id || null, title: item.term || item.content, content: item.content }, el);
      });
      break;
    }
    case "example": {
      console.log("Building example:", item.content.slice(0, 50));
      const label = document.createElement("span");
      label.className = "example-label";
      label.textContent = "Example";

      const text = document.createElement("p");
      text.className = "example-text";

      // Guard: only build if not already populated
      if (text.childNodes.length === 0) {
        buildExampleNodes(item.content, text);
      }

      el.appendChild(label);
      el.appendChild(text);
      break;
    }
    default: {
      // text
      el.textContent = item.content;
      break;
    }
  }

  return el;
}

function buildExampleNodes(content, container) {
  let s = content.replace(/^Example:\s*/i, "");

  // Step 1: Normalize \\command → \command
  s = s.replace(/\\\\([a-zA-Z{])/g, (_, c) => "\\" + c);

  // Step 2: Fix \% → %
  s = s.replace(/\\%/g, "%");

  // Step 3: CRITICAL — escape currency BEFORE any other processing
  // $NUMBER is currency only if followed by space+plain-word (not a LaTeX \command),
  // punctuation, or end of string
  s = s.replace(
    /\$(\d+(?:\.\d+)?)(?![.:]\d)(?=\s+(?![a-zA-Z]*\\)[a-zA-Z]|[.,;:!?]|\s*$)/g,
    (_, n) => `\x00CURRENCY${n}\x00`
  );

  // Step 4: Protect \$ escaped dollars
  s = s.replace(/\\\$/g, "\x00DOLLAR\x00");

  // Step 5: Now protect math $...$ blocks BEFORE fixing \DIGIT
  // so \overline{3} doesn't get digit-stripped
  const protected_ = [];
  s = s.replace(/\$([^$]+)\$/g, (m, inner) => {
    protected_.push(inner);
    return `\x00MATH${protected_.length - 1}\x00`;
  });

  // Step 6: Fix bare \DIGIT → DIGIT (only outside math blocks)
  s = s.replace(/\\(\d)/g, (_, d) => d);

  // Step 7: Restore math blocks and build parts list
  const parts = [];
  // Split on math placeholders
  const placeholderRe = /\x00MATH(\d+)\x00/g;
  let last = 0, pm;
  while ((pm = placeholderRe.exec(s)) !== null) {
    if (pm.index > last) parts.push({ type: "text", val: s.slice(last, pm.index) });
    parts.push({ type: "math", val: protected_[+pm[1]] });
    last = pm.index + pm[0].length;
  }
  if (last < s.length) parts.push({ type: "text", val: s.slice(last) });

  // Step 8: Render each part
  parts.forEach(part => {
    if (part.type === "math") {
      const span = document.createElement("span");
      try {
        katex.render(part.val, span, { throwOnError: false, displayMode: false });
      } catch(e) {
        span.textContent = part.val;
      }
      container.appendChild(span);
    } else {
      const text = part.val
        .replace(/\x00CURRENCY(\d+(?:\.\d+)?)\x00/g, (_, n) => "$" + n)
        .replace(/\x00DOLLAR\x00/g, "$");
      container.appendChild(document.createTextNode(text));
    }
  });
}

// elements fit to slide after animation
// also make title text smaller if > 1 line
function fitSlideTitle(slideEl) {
  const title = slideEl.querySelector(".slide-title");
  if (!title) return;

  title.style.fontSize = "";
  const lineH = parseInt(getComputedStyle(title).lineHeight);

  // More aggressive size ladder — keeps going smaller for very long titles
  const sizes = ["1.4rem", "1.2rem", "1.05rem", "0.95rem", "0.85rem"];
  for (const size of sizes) {
    if (title.scrollHeight <= lineH * 1.3) break;
    title.style.fontSize = size;
  }
}

function fitSlideContent(slideEl) {
  const body = slideEl.querySelector(".slide-body");
  if (!body) return;

  // Reset everything
  body.style.transform = "";
  body.style.transformOrigin = "";
  body.style.fontSize = "";
  body.style.marginBottom = "";

  const blockEls = slideEl.querySelectorAll(".body-equation, .body-definition, .body-example");
  blockEls.forEach(el => { el.style.padding = ""; });

  // ── Step 1: iteratively shrink font + padding, forcing reflow each step ──────
  const fontSizes = ["0.95rem", "0.88rem", "0.82rem", "0.76rem", "0.70rem", "0.64rem", "0.58rem"];
  const padSizes  = [null, null, "7px 12px", "6px 11px", "5px 10px", "4px 8px", "3px 6px"];

  for (let i = 0; i < fontSizes.length; i++) {
    // Force reflow so scrollHeight is accurate BEFORE checking
    void body.offsetHeight;
    if (body.scrollHeight <= body.clientHeight) break;

    body.style.fontSize = fontSizes[i];
    if (padSizes[i]) blockEls.forEach(el => { el.style.padding = padSizes[i]; });
  }

  // ── Step 2: scale transform for any remaining overflow ───────────────────────
  void body.offsetHeight; // reflow before final measurement
  if (body.scrollHeight <= body.clientHeight) return;

  const availableH = body.clientHeight;
  const naturalH   = body.scrollHeight;
  const scale      = availableH / naturalH;

  if (scale < 0.45) return;

  body.style.transformOrigin = "top left";
  body.style.transform       = `scale(${scale})`;
  body.style.marginBottom    = `-${naturalH * (1 - scale)}px`;
}

function fitSlideToStage() {
  const stage   = document.getElementById("stage");
  const scaler  = document.getElementById("slide-scaler");
  if (!stage || !scaler) return;

  const slideW  = 900;
  const slideH  = 540;
  const padH    = 72;  // top + bottom stage padding
  const stageW  = stage.clientWidth  - 48; // 24px padding each side
  const stageH  = stage.clientHeight - padH;

  const scaleByW = stageW / slideW;
  const scaleByH = stageH / slideH;
  const scale    = Math.min(1, scaleByW, scaleByH);

  // only apply if notes aren't already controlling the scaler
  if (!notesVisible) {
    if (scale < 1) {
      scaler.style.transform       = `scale(${scale})`;
      scaler.style.transformOrigin = "top center";
      scaler.style.marginBottom    = `-${slideH * (1 - scale)}px`;
    } else {
      scaler.style.transform    = "";
      scaler.style.marginBottom = "";
    }
  }
}

// ─── Sidebar toggle ───────────────────────────────────────────────────────────
let sidebarCollapsed = false;

function toggleSidebar() {
  sidebarCollapsed = !sidebarCollapsed;
  const slideshow = document.getElementById("slideshow");
  const btn = document.getElementById("sidebar-toggle");

  slideshow.classList.toggle("sidebar-collapsed", sidebarCollapsed);

  // ‹ when open, › when closed
  btn.innerHTML = sidebarCollapsed ? "&#8250;" : "&#8249;";
  btn.setAttribute("aria-label", sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar");

  // refit slide after grid transition completes
  setTimeout(fitSlideToStage, 220);
}

// ─── Animations ───────────────────────────────────────────────────────────────
/*
function scheduleAutoAnimations(slideEl, slide) {
  const hints = slide.animation_hints || [];
  // sort by order
  const sorted = [...hints].sort((a, b) => a.order - b.order);
  const onClickHints = hints.filter(h => h.trigger === "on_click");

  // immediately fire "with_previous" and first "after_previous" chain
  let delay = 100;
  sorted.forEach(hint => {
   if (hint.trigger === "with_previous") {
      setTimeout(() => applyAnimation(slideEl, hint), delay);
    } else if (hint.trigger === "after_previous") {
      delay += 450;
      setTimeout(() => applyAnimation(slideEl, hint), delay);
    } else if (hint.trigger === "on_click" && onClickHints.length === 1){
      delay += 200;
      setTimeout(() => applyAnimation(slideEl, hint), delay);
    }
  });
}
  */
// AFTER
function scheduleAutoAnimations(slideEl) {
  const items = slideEl.querySelectorAll("[data-body-index]");
  const timeouts = [];

  fitSlideTitle(slideEl);
  fitSlideContent(slideEl);

  const totalDuration = 300 + (items.length - 1) * 500 + 50; // last item + small buffer

  items.forEach((item, i) => {
    const t = setTimeout(() => {
      item.classList.remove("anim-hidden");
      item.classList.add("anim-fade_in");
      if (i === items.length - 1) {
        slideAnimating = false;  // ← set false after last item reveals
      }
    }, 300 + i * 500);
    timeouts.push(t);
  });

  // click anywhere to instantly finish all animations
  slideEl.addEventListener("click", () => {
    timeouts.forEach(t => clearTimeout(t));
    items.forEach(item => {
      item.classList.remove("anim-hidden");
      item.classList.add("anim-fade_in");
    });
    fitSlideContent(slideEl);
    slideAnimating = false;  // ← also set false on instant-finish
  }, { once: true });

  // edge case: slide has no body items
  if (items.length === 0) slideAnimating = false;
}

function fireNextOnClick(slideEl, slide) {
  // called on click/next — fires the next "on_click" animation if any remain
  const hints = (slide.animation_hints || [])
    .filter(h => h.trigger === "on_click")
    .sort((a, b) => a.order - b.order);

  if (animationStep < hints.length) {
    applyAnimation(slideEl, hints[animationStep]);
    animationStep++;
    return true; // consumed the click
  }
  return false; // no animations left, advance slide
}

function applyAnimation(slideEl, hint) {
  const target = slideEl.querySelector(`[data-body-index="${hint.target}"]`);
  if (!target) return;

  target.classList.remove("anim-hidden");
  target.classList.add(`anim-${hint.effect}`);
}

// ─── Navigation ───────────────────────────────────────────────────────────────
function navigate(dir) {
  const slide = slides[current];
  const slideEl = document.querySelector(".slide.active");

  // if navigating forward, check if there are pending on_click animations first
  /*
  if (dir === 1 && slideEl) {
    const consumed = fireNextOnClick(slideEl, slide);
    if (consumed) return;
  }
  */

  const next = current + dir;
  if (next < 0 || next >= slides.length) return;
  renderSlide(next);
  closeInfoPanel();
}

function handleKeyboard(e) {
  if (e.key === "ArrowRight" || e.key === "ArrowDown" || e.key === " ") navigate(1);
  if (e.key === "ArrowLeft" || e.key === "ArrowUp") navigate(-1);
}


function saveProgress(index, completed) {
  clearTimeout(progressSaveTimer);
  progressSaveTimer = setTimeout(() => {
    fetch(`/api/lessons/${lessonId}/progress`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ last_viewed_slide: index, completed })
    }).catch(err => console.warn("Could not save progress:", err.message));
  }, 500);
}

// ─── Chrome updates ───────────────────────────────────────────────────────────
function updateChrome(index) {
  const slide = slides[index];
  const total = slides.length;

  document.getElementById("slide-counter").textContent = `${index + 1} / ${total}`;

  saveProgress(index, index === total - 1);

  // type badge
  const badge = document.getElementById("type-badge");
  badge.textContent = slide.type.replace("_", " ");
  badge.className = `type-badge badge-${slide.type}`;

  // dot nav
  document.querySelectorAll(".dot").forEach((dot, i) => {
    dot.classList.toggle("dot-active", i === index);
  });

  // prev/next button states
  document.getElementById("btn-prev").disabled = index === 0;
  document.getElementById("btn-next").disabled = index === total - 1;
}

function buildDotNav() {
  const nav = document.getElementById("dot-nav-inner");
  nav.innerHTML = "";
  slides.forEach((_, i) => {
    const dot = document.createElement("button");
    dot.className = "dot";
    dot.innerHTML = `
    <span class="thumb-number">${i + 1}</span>
    <span class="thumb-title">${slides[i].title}</span>
    `;
    dot.setAttribute("aria-label", `Go to slide ${i + 1}`);
    dot.onclick = () => renderSlide(i);
    nav.appendChild(dot);
  });
}

// ─── Speaker notes ────────────────────────────────────────────────────────────
function toggleNotes(slideEl) {
  const notes = slideEl.querySelector(".speaker-notes");
  if (notes) notes.classList.toggle("hidden");
}

let notesVisible = false;

function updateNotesPanel(slide) {
  ttsRequestId++;
  const content = document.getElementById("notes-content");
  const btn = document.getElementById("notes-toggle-btn");
  const narrateBtn = document.getElementById("tts-narrate-btn");
  const notes = slide.speaker_notes || "";

  // stop any playing audio on slide change
  if (currentAudio) {
    currentAudio.pause();
    currentAudio = null;
    audioPaused = false;
    clearWordHighlights();
  }

  if (autoPlayTimer) {
    clearTimeout(autoPlayTimer);
    autoPlayTimer = null;
  }

  narrateBtn.disabled = !notes;
  document.getElementById("tts-skip-start-btn").disabled = !notes;
  document.getElementById("tts-skip-end-btn").disabled = !notes;

  if (notes) {
    content.textContent = notes;

    // Show as "playing/loading" immediately — TTS will auto-play
    narrateBtn.textContent = "■";
    narrateBtn.classList.add("tts-btn-active");

    // Clicking during load/autoplay-pending = cancel and pause
    narrateBtn.onclick = () => cancelAutoPlay(notes, narrateBtn);
  } else {
    narrateBtn.textContent = "▶";
    narrateBtn.classList.remove("tts-btn-active");
    narrateBtn.onclick = null;
    content.textContent = "";
    if (notesVisible) setNotesVisible(false);
  }
}

function cancelAutoPlay(notes, btn) {
  // Cancel pending autoplay timer
  if (autoPlayTimer) {
    clearTimeout(autoPlayTimer);
    autoPlayTimer = null;
  }

  // Pause if already playing
  if (currentAudio && !currentAudio.paused) {
    currentAudio.pause();
    audioPaused = true;
  }

  // Invalidate any in-flight fetch
  ttsRequestId++;

  // Reset button to paused state — clicking again resumes/plays
  btn.textContent = "▶";
  btn.classList.remove("tts-btn-active");
  btn.onclick = () => speakNotes(notes, btn);
}

function cacheGet(key) {
  if (!ttsCache.has(key)) return null;
  const url = ttsCache.get(key);
  ttsCache.delete(key);
  ttsCache.set(key, url);   // re-insert → moves it to the most-recent end
  return url;
}

function cacheSet(key, url) {
  ttsCache.set(key, url);
  if (ttsCache.size > TTS_CACHE_LIMIT) {
    const oldestKey = ttsCache.keys().next().value;   // first key = least recently used
    URL.revokeObjectURL(ttsCache.get(oldestKey));      // free the evicted blob
    ttsCache.delete(oldestKey);
  }
}

async function prefetchTTS(text) {
  if (!text || ttsCache.has(text)) return;
  try {
    const res = await fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text })
    });
    if (!res.ok) return;
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    cacheSet(text, url);
  } catch (err) {
    console.error("TTS prefetch failed:", err);
  }
}

async function speakNotes(text, btn) {
  // if playing → pause
  if (currentAudio && !audioPaused) {
    currentAudio.pause();
    audioPaused = true;
    btn.textContent = "▶";
    btn.classList.remove("tts-btn-active");
    return;
  }

  // if paused → resume
  if (currentAudio && audioPaused) {
    currentAudio.play();
    audioPaused = false;
    btn.textContent = "■";
    btn.classList.add("tts-btn-active");
    return;
  }

  // Fresh start
  const cachedUrl = cacheGet(text);
  if (cachedUrl) {
    // Restore normal toggle behavior now that we're actually playing
    btn.onclick = () => speakNotes(text, btn);
    startAudio(cachedUrl, text, btn);
    return;
  }

  const requestId = ttsRequestId;   // snapshot before the async gap
  btn.textContent = "■";
  btn.classList.add("tts-btn-active");
  btn.disabled = true;

  try {
    const res = await fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text })
    });
    if (!res.ok) throw new Error(`TTS error ${res.status}`);

    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    cacheSet(text, url);      // ← cache it before first play

    if (requestId !== ttsRequestId) return;   // slide changed mid-load; still cached, just don't play it
    btn.onclick = () => speakNotes(text, btn);
    startAudio(url, text, btn);
  } catch (err) {
    if (requestId !== ttsRequestId) return;    // don't touch a button that now belongs to a different slide
    console.error(err);
    btn.textContent = "▶";
    btn.classList.remove("tts-btn-active");
    btn.disabled = false;
  }
}

function startAudio(url, text, btn) {
  // Touch the cache entry so it's marked as most recently USED
  // (prefetch inserted it, but playing it should refresh its LRU position)
  cacheGet(text);  // moves to most-recent end; return value ignored since we have url already

  currentAudio = new Audio(url);
  currentAudio.playbackRate = currentSpeed;
  currentAudio.volume = currentVolume;
  audioPaused = false;

  const muteBtn = document.getElementById("tts-mute-btn");
  currentAudio.muted = muteBtn.classList.contains("tts-btn-active");

  currentAudio.addEventListener("loadedmetadata", () => {
    if (!notesVisible) setNotesVisible(true);
    const { sentences, boundaries } = computeSentenceBoundaries(text, currentAudio.duration);
    renderSentences(sentences);
    sentenceBoundaries = boundaries;
    currentSentenceIndex = -1;
  });

  currentAudio.addEventListener("play", startHighlightLoop);
  currentAudio.addEventListener("pause", stopHighlightLoop);

  currentAudio.onended = () => finishNarration(btn);

  btn.textContent = "■";
  btn.classList.add("tts-btn-active");
  btn.disabled = false;
  currentAudio.play();

  // prefetch next slide in background
  const nextSlide = slides[current + 1];
  if (nextSlide?.speaker_notes) {
    prefetchTTS(nextSlide.speaker_notes);
  }
}

function finishNarration(btn) {
  btn.textContent = "▶";
  btn.classList.remove("tts-btn-active");
  currentAudio = null;
  audioPaused = false;
  clearWordHighlights();
}

function stopNarrationFully() {
  if (currentAudio) {
    currentAudio.pause();
    currentAudio = null;
  }
  audioPaused = false;
  if (autoPlayTimer) {
    clearTimeout(autoPlayTimer);
    autoPlayTimer = null;
  }
  ttsRequestId++; // invalidate any in-flight prefetch/autoplay
  clearWordHighlights();
  const btn = document.getElementById("tts-narrate-btn");
  if (btn) {
    btn.textContent = "▶";
    btn.classList.remove("tts-btn-active");
  }
}

function skipToStart() {
  if (!currentAudio) return;
  currentAudio.currentTime = 0;
  currentSentenceIndex = -1;
  highlightSentenceAt(0); // reflects instantly even if paused, since the rAF loop won't be running
}

function skipToEnd() {
  if (!currentAudio) return;
  const btn = document.getElementById("tts-narrate-btn");
  currentAudio.pause();
  finishNarration(btn);
}

function computeSentenceBoundaries(text, duration) {
  const sentences = text.match(/[^.!?]+[.!?]+[\s]*/g) || [text];
  const wordCounts = sentences.map(s => s.trim().split(/\s+/).length);
  const totalWords = wordCounts.reduce((a, b) => a + b, 0);

  let offset = 0;
  const boundaries = wordCounts.map(count => {
    const dur = (count / totalWords) * duration;
    const b = { start: offset, end: offset + dur };
    offset += dur;
    return b;
  });

  return { sentences, boundaries };
}

function renderSentences(sentences) {
  const content = document.getElementById("notes-content");
  content.innerHTML = "";
  sentences.forEach((sentence, i) => {
    const span = document.createElement("span");
    span.className = "sentence-token";
    span.dataset.sentence = i;
    span.textContent = sentence;
    content.appendChild(span);
  });
}

function highlightSentenceAt(time) {
  let idx = sentenceBoundaries.findIndex(b => time < b.end);
  if (idx === -1) idx = sentenceBoundaries.length - 1;
  if (idx === currentSentenceIndex) return; // avoid redundant DOM work every frame

  currentSentenceIndex = idx;
  const content = document.getElementById("notes-content");
  content.querySelectorAll(".sentence-token").forEach(s => s.classList.remove("sentence-active"));
  const span = content.querySelector(`[data-sentence="${idx}"]`);
  if (span) {
    span.classList.add("sentence-active");
    span.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

function tickHighlight() {
  if (!currentAudio || currentAudio.paused) { highlightRAF = null; return; }
  highlightSentenceAt(currentAudio.currentTime);
  highlightRAF = requestAnimationFrame(tickHighlight);
}

function startHighlightLoop() {
  if (!highlightRAF) highlightRAF = requestAnimationFrame(tickHighlight);
}

function stopHighlightLoop() {
  if (highlightRAF) cancelAnimationFrame(highlightRAF);
  highlightRAF = null;
}

function clearWordHighlights() {
  stopHighlightLoop();
  currentSentenceIndex = -1;
  sentenceBoundaries = [];
  const content = document.getElementById("notes-content");
  if (content) content.textContent = content.innerText;
}

function skipToStart() {
  if (!currentAudio) return;
  currentAudio.currentTime = 0;
  currentSentenceIndex = -1;
  highlightSentenceAt(0);
}

function skipToEnd() {
  if (!currentAudio) return;
  const btn = document.getElementById("tts-narrate-btn");
  currentAudio.pause();
  finishNarration(btn);
}

function toggleNotes() {
  setNotesVisible(!notesVisible);
}

const NOTES_PANEL_H = 140; // must match CSS
const NOTES_BAR_H   = 28;
const STAGE_PAD     = 32;  // 16px top + 16px bottom equivalent

function setNotesVisible(visible) {
  notesVisible = visible;

  const panel   = document.getElementById("notes-panel");
  const btn     = document.getElementById("notes-toggle-btn");
  const stage   = document.getElementById("stage");
  const scaler  = document.getElementById("slide-scaler");

  panel.classList.toggle("hidden", !visible);
  btn.classList.toggle("notes-active", visible);

  if (visible) {
    const stageH    = stage.getBoundingClientRect().height;
    const stageW    = stage.getBoundingClientRect().width - 48;
    const slideH    = 540;
    const slideW    = 900;

    const baseScale   = Math.min(1, stageW / slideW);          // current fit scale
    const available   = stageH - NOTES_PANEL_H - NOTES_BAR_H - STAGE_PAD;
    const scaleByH    = available / slideH;
    const scaleByW    = stageW / slideW;
    const scale       = Math.min(baseScale, scaleByH, scaleByW);

    scaler.style.transform       = `scale(${scale})`;
    scaler.style.transformOrigin = "top center";
    scaler.style.marginBottom    = `${-(slideH * (1 - scale))}px`;
  } else {
    scaler.style.transform    = "";
    scaler.style.marginBottom = "";
    fitSlideToStage();   // restore the window-fit scale
  }
}

// ─── Term/Equation Info ────────────────────────────────────────────────────────────
async function openInfoPanel(opts, sourceEl) {
  const { panelType, title, content } = opts;

  // toggle closed if clicking the same element again
  if (infoPanelOpen && activeInfoElement === sourceEl) {
    closeInfoPanel();
    return;
  }

  // mark source element as active
  document.querySelectorAll(".info-active").forEach(el => el.classList.remove("info-active"));
  sourceEl.classList.add("info-active");
  activeInfoElement = sourceEl;
  infoPanelOpen = true;

  const slideshow = document.getElementById("slideshow");
  slideshow.classList.add("info-panel-open");

  // set card accent + type badge
  const card = document.getElementById("info-card");
  card.className = "";
  card.classList.add(`info-type-${panelType}`);
  card.style.setProperty("--info-accent", panelType === "equation" ? "var(--accent)" : "var(--yellow)");

  document.getElementById("info-card-type-badge").textContent =
    panelType === "equation" ? "Equation" : "Definition";  // ← panelType, not data.type

  // show loading state immediately
  document.getElementById("info-card-term").textContent = panelType === "equation" ? "" : title;
  document.getElementById("info-card-content").innerHTML = `
    <div class="info-loading">
      <div class="info-loading-bar"></div>
      <span>Loading...</span>
    </div>`;

  setTimeout(() => {
    fitSlideToStage();
    if (notesVisible) setNotesVisible(true);
  }, 50);

  // build context from current slide
  const slide = slides[current];
  const slideTitle = slide?.title || "";
  const courseName = document.getElementById("course-name")?.textContent || "";
  const slideText = (slide?.body || [])
    .filter(item => item.type === "text")
    .map(item => item.content)
    .join(" ");

  try {
    const endpoint = panelType === "equation" 
      ? `/api/lessons/${lessonId}/info/equation` 
      : `/api/lessons/${lessonId}/info/definition`;
    const payload  = panelType === "equation"
      ? { latex: content, context: `${slideTitle} — ${courseName}`, slide_text: slideText }
      : { term: title, definition_on_slide: content, context: `${slideTitle} — ${courseName}`, slide_text: slideText };

    const res = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    if (panelType === "definition") renderDefinitionCard(data);
    else                            renderEquationCard(data);

  } catch (err) {
    document.getElementById("info-card-content").innerHTML =
      `<p class="info-error">Could not load info. Please try again.</p>`;
    console.error("Info panel fetch failed:", err);
  }
}

function closeInfoPanel() {

  const slideshow = document.getElementById("slideshow");

  slideshow.classList.remove("info-panel-open");

  infoPanelOpen = false;
  selectedConcept = null;

  if (activeInfoElement) {
    activeInfoElement.classList.remove("info-active");
    activeInfoElement = null;
  }

  setTimeout(() => {
    fitSlideToStage();

    if (notesVisible) {
      setNotesVisible(true);
    }
  }, 50);
}


function renderDefinitionCard(data) {
  const termEl = document.getElementById("info-card-term");
  termEl.innerHTML = "";

  const row = document.createElement("div");
  row.className = "info-term-row";

  const termText = document.createElement("span");
  termText.className = "info-term-text";
  termText.textContent = data.term;
  row.appendChild(termText);

  const pronounceBtn = document.createElement("button");
  pronounceBtn.className = "info-pronounce-btn";
  pronounceBtn.innerHTML = "🔊";
  pronounceBtn.setAttribute("aria-label", `Pronounce "${data.term}"`);
  pronounceBtn.onclick = () => pronounceTerm(data.term, pronounceBtn);
  row.appendChild(pronounceBtn);

  termEl.appendChild(row);

  const content = document.getElementById("info-card-content");
  content.innerHTML = "";

  // part of speech badge + definition
  const pos = document.createElement("span");
  pos.className = "info-pos-badge";
  pos.textContent = data.part_of_speech;
  content.appendChild(pos);

  const def = document.createElement("p");
  def.className = "info-definition-text";
  def.textContent = data.definition;
  content.appendChild(def);

  // divider
  content.appendChild(makeDivider());

  // examples
  const exHeading = document.createElement("p");
  exHeading.className = "info-section-heading";
  exHeading.textContent = "Examples";
  content.appendChild(exHeading);

  data.examples.forEach((ex, i) => {
    const row = document.createElement("div");
    row.className = "info-example-row";

    const num = document.createElement("span");
    num.className = "info-example-num";
    num.textContent = i + 1;

    const text = document.createElement("span");
    text.className = "info-example-text";

    // split on [[...]] markers and highlight the wrapped term
    const parts = ex.split(/\[\[([^\]]+)\]\]/);
    parts.forEach((part, pi) => {
      if (pi % 2 === 1) {
        // odd indices are the captured term
        const highlight = document.createElement("span");
        highlight.className = "info-example-term";
        highlight.textContent = part;
        text.appendChild(highlight);
    } else if (part) {
      text.appendChild(document.createTextNode(part));
    }
  });

  row.appendChild(num);
  row.appendChild(text);
  content.appendChild(row);
});

  renderMathInCard();
}

async function pronounceTerm(text, btn) {
  if (pronounceAudio) {
    pronounceAudio.pause();
    pronounceAudio = null;
  }
  document.querySelectorAll(".info-pronounce-btn.tts-speaking")
    .forEach(b => b.classList.remove("tts-speaking"));

  const cached = pronounceCache.get(text);
  if (cached) {
    // Move to most-recent end (LRU touch)
    pronounceCache.delete(text);
    pronounceCache.set(text, cached);
    playPronunciation(cached, btn);
    return;
  }

  btn.disabled = true;
  try {
    const res = await fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text })
    });
    if (!res.ok) throw new Error(`TTS error ${res.status}`);

    const blob = await res.blob();
    const url = URL.createObjectURL(blob);

    // LRU insert with eviction
    pronounceCache.set(text, url);
    if (pronounceCache.size > PRONOUNCE_CACHE_LIMIT) {
      const oldestKey = pronounceCache.keys().next().value;
      URL.revokeObjectURL(pronounceCache.get(oldestKey));  // free the blob
      pronounceCache.delete(oldestKey);
    }

    playPronunciation(url, btn);
  } catch (err) {
    console.error(err);
  } finally {
    btn.disabled = false;
  }
}

function playPronunciation(url, btn) {
  pronounceAudio = new Audio(url);
  btn.classList.add("tts-speaking");
  pronounceAudio.onended = () => {
    btn.classList.remove("tts-speaking");
    pronounceAudio = null;
  };
  pronounceAudio.play();
}

function renderEquationCard(data) {
  const latex = data.latex.replace(/\\\\/g, "\\");  // normalize at entry point

  // title = the equation itself rendered in LaTeX
  const termEl = document.getElementById("info-card-term");
  termEl.innerHTML = ""; //clear first

    // If latex contains \text{...} prefix (LLM label + equation pattern),
  // split it: render the text part as a heading, equation part separately.
  // Pattern: \text{some label} = <equation> or just a pure equation
  const textPrefixMatch = latex.match(/^\\text\{([^}]+)\}\s*=\s*(.+)$/s);

  if (textPrefixMatch) {
    // text label goes in a <div class="info-term-label">
    const labelEl = document.createElement("div");
    labelEl.className = "info-term-label";
    labelEl.textContent = textPrefixMatch[1];  // plain text, no KaTeX needed
    termEl.appendChild(labelEl);

    // equation goes in a separate contained block
    const eqEl = document.createElement("div");
    eqEl.className = "info-term-equation";
    if (window.katex) {
      try {
        eqEl.innerHTML = katex.renderToString(textPrefixMatch[2], {
          displayMode: true,
          throwOnError: false
        });
      } catch(e) {
        eqEl.textContent = textPrefixMatch[2];
      }
    } else {
      eqEl.textContent = textPrefixMatch[2];
    }
    termEl.appendChild(eqEl);

  } else {// render the title immediately and separately — KaTeX may skip it
  // if the card body was previously rendered and marked nodes as processed
    if (window.katex) {
        try {
            termEl.innerHTML = katex.renderToString(latex, { displayMode: true, throwOnError: false });
        } catch(e) {
            termEl.textContent = latex;
        }
    } else {
        termEl.textContent = latex;
    }
  }  

  const content = document.getElementById("info-card-content");
  content.innerHTML = "";

  // description
  const desc = document.createElement("p");
  desc.className = "info-definition-text";
  desc.textContent = data.description;
  content.appendChild(desc);

  content.appendChild(makeDivider());

  // variables
  if (data.variables?.length) {
    const varHeading = document.createElement("p");
    varHeading.className = "info-section-heading";
    varHeading.textContent = "Variables";
    content.appendChild(varHeading);

    data.variables.forEach(v => {
        const row = document.createElement("div");
        row.className = "info-var-row";

        const symbol = document.createElement("span");
        symbol.className = "info-var-symbol";

        // Extract plain text from \text{...} — KaTeX would render this nowrap
        const textMatch = v.symbol.match(/^\\text\{([^}]+)\}$/);
        const isPlainText = textMatch || (!v.symbol.includes("\\") && (v.symbol.includes(" ") || v.symbol.length > 8));

        if (isPlainText) {
            symbol.textContent = textMatch ? textMatch[1] : v.symbol;  // unwrap \text{} if present
            symbol.classList.add("info-var-symbol-text");
        } else {
            symbol.textContent = `$${v.symbol}$`;
        }

        const meaning = document.createElement("span");
        meaning.className = "info-var-meaning";
        meaning.textContent = v.meaning;

        row.appendChild(symbol);
        row.appendChild(meaning);
        content.appendChild(row);
    });
  }

  // constraints
  if (data.constraints?.length) {
    content.appendChild(makeDivider());
    const cHeading = document.createElement("p");
    cHeading.className = "info-section-heading";
    cHeading.textContent = "Constraints";
    content.appendChild(cHeading);

    data.constraints.forEach(c => {
      const row = document.createElement("p");
      row.className = "info-constraint-row";
      row.textContent = c;
      content.appendChild(row);
    });
  }

  content.appendChild(makeDivider());

  // examples — collapsible
  const exHeading = document.createElement("p");
  exHeading.className = "info-section-heading";
  exHeading.textContent = "Worked Examples";
  content.appendChild(exHeading);

  data.examples.forEach((ex, i) => {
    const block = document.createElement("div");
    block.className = "info-eq-example";

    const question = document.createElement("button");
    question.className = "info-eq-example-q";
    question.innerHTML = `<span class="info-example-num">${i + 1}</span><span class="info-eq-example-text"></span><span class="info-eq-chevron">▸</span>`;
    question.querySelector(".info-eq-example-text").textContent = ex.problem;

    const steps = document.createElement("div");
    steps.className = "info-eq-steps hidden";
    ex.steps.forEach((step, si) => {
      const stepEl = document.createElement("div");
      stepEl.className = "info-eq-step";
      stepEl.innerHTML = `<span class="info-step-num">${si + 1}</span><span class="info-step-text"></span>`;
      stepEl.querySelector(".info-step-text").textContent = step;
      steps.appendChild(stepEl);
    });

    question.addEventListener("click", () => {
      const isOpen = !steps.classList.contains("hidden");
      steps.classList.toggle("hidden", isOpen);
      question.querySelector(".info-eq-chevron").textContent = isOpen ? "▸" : "▾";
      block.classList.toggle("info-eq-example-open", !isOpen);
      if (!isOpen) renderMathInCard(); // re-render math in newly visible steps
    });

    block.appendChild(question);
    block.appendChild(steps);
    content.appendChild(block);
  });

  renderMathInCard();
}

function makeDivider() {
  const d = document.createElement("div");
  d.className = "info-divider";
  return d;
}

function renderMathInCard() {
  const card = document.getElementById("info-card");
  if (window.renderMathInElement) {
    renderMathInElement(card, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "$",  right: "$",  display: false },
      ],
      throwOnError: false,
    });
  }
}


// ─── Study Section  ────────────────────────────────────────────────────────────
function goBackFromProgress() {
  if (lastScene === "welcome") { showWelcome(); return; } //welcome isn't handled by switchScene
  switchScene(lastScene);
}

function switchScene(name) {
  if (name !== "progress" && name !== "lessons" && name !== "account") lastScene = name; // track last scene for Back button

  if (name === "study" || name === "slideshow") {
    const url = new URL(window.location);
    url.searchParams.set("scene", name); // persist current scene across refresh
    history.replaceState(null, "", url);
    // Switching between study <-> slideshow only pauses — resuming on the
    // slides scene should pick up where you left off, not restart
    if (currentAudio) {
      currentAudio.pause();
      audioPaused = true;
    }
    if (autoPlayTimer) {
      clearTimeout(autoPlayTimer);
      autoPlayTimer = null;
    }
    ttsRequestId++;
    const btn = document.getElementById("tts-narrate-btn");
    if (btn) {
      btn.textContent = "▶";
      btn.classList.remove("tts-btn-active");
    }
  } else if (name === "progress" || name === "lessons") {
    // Just a detour, not leaving the lesson. pause only, so coming back
    // resumes narration instead of restarting the slide's notes
    if (currentAudio) {
      currentAudio.pause();
      audioPaused = true;
    }
    if (autoPlayTimer) {
      clearTimeout(autoPlayTimer);
      autoPlayTimer = null;
    }
    ttsRequestId++;
  } else {
    // Leaving the lesson context entirely (Account, Auth, etc.)
    // wipe narration instead of just pausing it
    stopNarrationFully();
  }
  
  document.getElementById("slideshow").classList.toggle("hidden", name !== "slideshow");
  document.getElementById("study").classList.toggle("hidden", name !== "study");
  document.getElementById("progress-screen").classList.toggle("hidden", name !== "progress");
  document.getElementById("welcome-screen").classList.toggle("hidden", name !== "welcome");
  document.getElementById("auth-screen")?.classList.toggle("hidden", name !== "auth");
  document.getElementById("lessons-screen").classList.toggle("hidden", name !== "lessons");
  document.getElementById("account-screen").classList.toggle("hidden", name !== "account");


  if (name === "progress") {
    loadProgressPage();
    const backBtn = document.getElementById("btn-to-slides-progress"); // sync label to target scene
    if (backBtn) {
      backBtn.textContent = "Back";
    }
  }

  if (name === "lessons") {
    loadLessonLibrary();
  }

  if (name === "account") {
    loadAccountPage();
  }

  if (name === "study" && curriculum) {
    // build "All Concepts" as index 0, then real concepts
    const allId = "__all__";
    const firstBtn = document.querySelector(`.concept-btn[data-concept-id="${allId}"]`);
    if (firstBtn) firstBtn.click();
    else {
      const firstReal = document.querySelector(".concept-btn");
      if (firstReal) firstReal.click();
    }
  }
}

// ─── Saved items state ─────────────────────────────────────────────────────
let savedItems = new Set(); // stores unique string IDs

function makeItemId(type, conceptId, idx) {
  return `${type}:${conceptId}:${idx}`;
}

function isSaved(id) {
  return savedItems.has(id);
}

function toggleSaved(id) {
  if (savedItems.has(id)) savedItems.delete(id);
  else savedItems.add(id);
  buildConceptNav(); // rebuild nav in case "Saved" needs to appear/disappear
}


async function loadCurriculum() {
  try {
    const res = await fetch(`/api/lessons/${lessonId}/curriculum`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    curriculum = data.curriculum_graph;

    document.getElementById("study-course-name").textContent = curriculum.course;
    buildConceptNav();

    // autoselect "All Concepts" when curriculum first loads
    selectConcept("__all__");
  } catch (err) {
    console.warn("Could not load curriculum:", err.message);
  }
}

function renderLatexContained(container, latex) {
  latex = latex.replace(/\\\\/g, "\\");

  const MATH_PATTERN = /\\frac|\\sqrt|\\int|\\sum|\\prod|\\pi|\\theta|\\alpha|\\beta|\\gamma|\\delta|\\lambda|\\mu|\\sigma|\\omega|\\phi|\\psi|\\rho|\\eta|\\epsilon|\\zeta|\\xi|\\chi|\\tau|\\nu|\\kappa|\\iota|\\left|\\right|\\cup|\\cap|\\in|\\notin|\\subset|\\subseteq|\\supset|\\supseteq|\\emptyset|\\varnothing|\\infty|\\partial|\\nabla|\\forall|\\exists|\\neg|\\wedge|\\vee|\\oplus|\\pm|\\mp|\\approx|\\sim|\\cong|\\equiv|\\propto|\\perp|\\parallel|\\angle|\\triangle|\\square|\\diamond|\\overline|\\overrightarrow|\\overleftarrow|\\vec|\\hat|\\bar|\\dot|\\ddot|\\tilde|\\log|\\ln|\\sin|\\cos|\\tan|\\sec|\\csc|\\cot|\\arcsin|\\arccos|\\arctan|\\sinh|\\cosh|\\tanh|\\lim|\\max|\\min|\\gcd|\\binom|\\pmatrix|\\matrix|\\begin|\\lfloor|\\rfloor|\\lceil|\\rceil|\\mid|\\nmid|\\vert|\\Vert|\\cdots|\\vdots|\\ddots|\\mathbb|\\mathbf|\\mathrm|\\mathit|\\mathcal|\^{|_{|\^[^}]|_[^}]/;

  const hasMath = MATH_PATTERN.test(latex);

  if (!hasMath) {
    const readable = latex
      .replace(/\\text\{([^}]*)\}/g, "$1")
      .replace(/\\Rightarrow/g, "⇒")
      .replace(/\\Leftarrow/g, "⇐")
      .replace(/\\Leftrightarrow/g, "⟺")
      .replace(/\\rightarrow/g, "→")
      .replace(/\\leftarrow/g, "←")
      .replace(/\\times/g, "×")
      .replace(/\\div/g, "÷")
      .replace(/\\cdot/g, "·")
      .replace(/\\neq/g, "≠")
      .replace(/\\leq/g, "≤")
      .replace(/\\geq/g, "≥")
      .replace(/\\ll/g, "≪")
      .replace(/\\gg/g, "≫")
      .replace(/\\ldots/g, "…")
      .replace(/\\quad/g, "  ")
      .replace(/\\qquad/g, "    ")
      .replace(/\\%/g, "%")
      .replace(/\\[a-zA-Z]+/g, "")
      .replace(/[{}]/g, "")
      .replace(/\s+/g, " ")
      .trim();

    container.classList.add("formula-text-only");
    container.textContent = readable;
    return;
  }

  if (window.katex) {
    try {
      container.innerHTML = katex.renderToString(latex, {
        displayMode: true,
        throwOnError: false,
        trust: false,
      });
    } catch (e) {
      container.textContent = latex;
    }
  } else {
    container.textContent = latex;
  }
}

function buildConceptNav() {
  const nav = document.getElementById("concept-nav");
  nav.innerHTML = "";

  // Saved — only if there are starred items
  if (savedItems.size > 0) {
    const savedBtn = document.createElement("button");
    savedBtn.className = "concept-btn";
    savedBtn.dataset.conceptId = "__saved__";

    const savedBadge = document.createElement("span");
    savedBadge.className = "importance-badge importance-all";
    savedBadge.textContent = "saved";

    const savedLabel = document.createElement("span");
    savedLabel.className = "concept-label";
    savedLabel.textContent = "Saved";

    savedBtn.appendChild(savedBadge);
    savedBtn.appendChild(savedLabel);
    savedBtn.onclick = () => selectConcept("__saved__");
    nav.appendChild(savedBtn);
  }

  // all concepts overview
  const allBtn = document.createElement("button");
  allBtn.className = "concept-btn";
  allBtn.dataset.conceptId = "__all__";

  const allBadge = document.createElement("span");
  allBadge.className = "importance-badge importance-all";
  allBadge.textContent = "all";

  const allLabel = document.createElement("span");
  allLabel.className = "concept-label";
  allLabel.textContent = "All Concepts";

  allBtn.appendChild(allBadge);
  allBtn.appendChild(allLabel);
  allBtn.onclick = () => selectConcept("__all__");
  nav.appendChild(allBtn);

  // actual concepts
  curriculum.concepts.forEach(concept => {
    const btn = document.createElement("button");
    btn.className = "concept-btn";
    btn.dataset.conceptId = concept.concept_id;

    const badge = document.createElement("span");
    badge.className = `importance-badge importance-${concept.importance}`;
    badge.textContent = concept.importance;

    const label = document.createElement("span");
    label.className = "concept-label";
    label.textContent = concept.name;

    btn.appendChild(badge);
    btn.appendChild(label);
    btn.onclick = () => selectConcept(concept.concept_id);
    nav.appendChild(btn);
  });
}

function buildSavedConcept() {
  const key_terms = [];
  const formulas = [];
  const flashcards = [];
  const quizzes = [];

  curriculum.concepts.forEach(concept => {
    (concept.study?.key_terms || []).forEach((kt, idx) => {
      const id = makeItemId("keyterm", concept.concept_id, idx);
      if (isSaved(id)) key_terms.push({ ...kt, _savedId: id });
    });
    (concept.study?.formulas || []).forEach((f, idx) => {
      const id = makeItemId("formula", concept.concept_id, idx);
      if (isSaved(id)) formulas.push({ ...f, _savedId: id });
    });
    (concept.study?.flashcards || []).forEach((fc, idx) => {
      const id = makeItemId("flashcard", concept.concept_id, idx);
      if (isSaved(id)) flashcards.push({ ...fc, _savedId: id });
    });
    (concept.study?.quiz_questions || []).forEach((q, idx) => {
      const id = makeItemId("quiz", concept.concept_id, idx);
      if (isSaved(id)) quizzes.push({ ...q, _savedId: id });
    });
  });

  return {
    concept_id: "__saved__",
    name: "Saved",
    description: "Key terms and formulas you've starred for extra review.",
    importance: "all",
    study: { key_terms, formulas, flashcards, quiz_questions: quizzes }
  };
}

function selectConcept(id) {
  activeConceptId = id;

  // highlight active nav item
  document.querySelectorAll(".concept-btn").forEach(btn => {
    btn.classList.toggle("concept-btn-active", btn.dataset.conceptId === id);
  });

  document.getElementById("concept-placeholder").classList.add("hidden");
  const detail = document.getElementById("concept-detail");
  detail.classList.remove("hidden");
  detail.innerHTML = "";

  // build the concept object — real or synthetic "all", "saved"
  const isAll = id === "__all__";
  const isSavedView = id === "__saved__";
  const concept = isAll ? buildAllConcept()
    : isSavedView ? buildSavedConcept()
    : curriculum.concepts.find(c => c.concept_id === id);


  // ── Fixed-height header zone ──
  const headerZone = document.createElement("div");
  headerZone.className = "concept-header-zone";

  const header = document.createElement("div");
  header.className = "concept-header";

  const name = document.createElement("h2");
  name.className = "concept-name";
  name.textContent = concept.name;

  const importancePill = document.createElement("span");
  importancePill.className = `importance-badge importance-${concept.importance}`;
  importancePill.textContent = concept.importance;

  const desc = document.createElement("p");
  desc.className = "concept-desc";
  desc.textContent = concept.description;

  header.appendChild(name);
  headerZone.appendChild(header);
  header.appendChild(importancePill);
  headerZone.appendChild(desc);
  detail.appendChild(headerZone);

  // ── Tabs row with Next button ──
  const tabsRow = document.createElement("div");
  tabsRow.className = "study-tabs-row";

  const tabs = document.createElement("div");
  tabs.className = "study-tabs";

  const panel = document.createElement("div");
  panel.className = "study-panel";

  ["Overview", "Flashcards", "Quiz"].forEach((label, i) => {
    const btn = document.createElement("button");
    btn.className = "study-tab" + (i === 0 ? " study-tab-active" : "");
    btn.textContent = label;
    btn.onclick = () => {
      document.querySelectorAll(".study-tab").forEach(t => t.classList.remove("study-tab-active"));
      btn.classList.add("study-tab-active");
      renderStudyPanel(concept, label.toLowerCase(), panel);
    };
    tabs.appendChild(btn);
  });

  // Prev / Next — include "Saved" only if it currently exists in the nav
  const navIds = [];
  if (savedItems.size > 0) navIds.push("__saved__");
  navIds.push("__all__", ...curriculum.concepts.map(c => c.concept_id));
  const currentIdx = navIds.indexOf(id);

  const navBtnGroup = document.createElement("div");
  navBtnGroup.className = "study-nav-btn-group";

  const prevBtn = document.createElement("button");
  prevBtn.className = "study-next-btn";
  prevBtn.textContent = "‹";
  prevBtn.setAttribute("aria-label", "Previous concept");
  prevBtn.disabled = currentIdx <= 0;
  prevBtn.onclick = () => {
    const prevId = navIds[currentIdx - 1];
    selectConcept(prevId);
    const prevNavBtn = document.querySelector(`.concept-btn[data-concept-id="${prevId}"]`);
    if (prevNavBtn) prevNavBtn.scrollIntoView({ block: "nearest" });
  };

  const nextBtn = document.createElement("button");
  nextBtn.className = "study-next-btn";
  nextBtn.textContent = "›";
  nextBtn.setAttribute("aria-label", "Next concept");
  nextBtn.disabled = currentIdx >= navIds.length - 1;
  nextBtn.onclick = () => {
    const nextId = navIds[currentIdx + 1];
    selectConcept(nextId);
    const nextNavBtn = document.querySelector(`.concept-btn[data-concept-id="${nextId}"]`);
    if (nextNavBtn) nextNavBtn.scrollIntoView({ block: "nearest" });
  };

  navBtnGroup.appendChild(prevBtn);
  navBtnGroup.appendChild(nextBtn);

  tabsRow.appendChild(tabs);
  tabsRow.appendChild(navBtnGroup);
  detail.appendChild(tabsRow);
  detail.appendChild(panel);

  renderStudyPanel(concept, "overview", panel);
}

function buildAllConcept() {

  const key_terms = [];
  const formulas = [];
  const flashcards = [];
  const quizzes = [];

  curriculum.concepts.forEach(concept => {
    (concept.study?.key_terms || []).forEach((kt, idx) => {
      key_terms.push({ ...kt, _savedId: makeItemId("keyterm", concept.concept_id, idx) });
    });
    (concept.study?.formulas || []).forEach((f, idx) => {
      formulas.push({ ...f, _savedId: makeItemId("formula", concept.concept_id, idx) });
    });
    (concept.study?.flashcards || []).forEach((fc, idx) => {
      flashcards.push({ ...fc, _savedId: makeItemId("flashcard", concept.concept_id, idx) });
    });
    (concept.study?.quiz_questions || []).forEach((q, idx) => {
      quizzes.push({ ...q, _savedId: makeItemId("quiz", concept.concept_id, idx) });
    });
  });

  return {
    concept_id: "__all__",
    name: "All Concepts",
    description: "A combined view of all key terms, flashcards, and quiz questions across every concept.",
    importance: "all",
    study: {
      key_terms,
      formulas,
      flashcards,
      quiz_questions: quizzes,
    }
  };
}

function toggleSaved(id) {
  if (savedItems.has(id)) savedItems.delete(id);
  else savedItems.add(id);
  buildConceptNav();
  if (activeConceptId === "__saved__" && savedItems.size === 0) {
    selectConcept("__all__"); // bounce back if Saved becomes empty
  }
}

function renderStudyPanel(concept, mode, panel) {
  panel.innerHTML = "";
  const s = concept.study;

  if (mode === "overview") {
    // Key Terms
    if (s.key_terms?.length) {
      panel.appendChild(sectionHeading("Key Terms"));
      s.key_terms.forEach((kt, idx) => {
        const row = document.createElement("div");
        row.className = "term-row";

        const termNameEl = document.createElement("span");
        termNameEl.className = "term-name";
        termNameEl.textContent = kt.term;
        const termDefEl = document.createElement("span");
        termDefEl.className = "term-def";
        termDefEl.textContent = kt.definition;
        row.append(termNameEl, termDefEl);

        const itemId = kt._savedId || makeItemId("keyterm", concept.concept_id, idx);
        row.appendChild(buildStarButton(itemId));
        panel.appendChild(row);
      });
    }

    // Formulas
    if (s.formulas?.length) {
      panel.appendChild(sectionHeading("Formulas"));
      s.formulas.forEach((f, idx) => {
        const block = document.createElement("div");
        block.className = "formula-block";
        
        const formulaLatex = document.createElement("div");
        formulaLatex.className = "formula-latex";
        const formulaExp = document.createElement("div");
        formulaExp.className = "formula-explanation";
        formulaExp.textContent = f.explanation;

        renderLatexContained(formulaLatex, f.latex);

        const itemId = f._savedId || makeItemId("formula", concept.concept_id, idx);
        block.appendChild(buildStarButton(itemId));
        block.appendChild(formulaLatex);
        block.appendChild(formulaExp);
        panel.appendChild(block);
      });
      /*
      if (window.renderMathInElement) {
        renderMathInElement(panel, {
          delimiters: [
            { left: "$$", right: "$$", display: true },
            { left: "$", right: "$", display: false },
          ],
          throwOnError: false,
        });
      }*/
    }
  }

  if (mode === "flashcards") {
    if (!s.flashcards?.length) {
      panel.innerHTML = `<p class="empty-state">No flashcards for this concept.</p>`;
      return;
    }
    let cardIndex = 0;
    let flipped = false;

    const render = () => {
      panel.innerHTML = "";
      const card = s.flashcards[cardIndex];
      flipped = false;

      const topRow = document.createElement("div");
      topRow.className = "quiz-header";

      const counter = document.createElement("div");
      counter.className = "card-counter";
      counter.textContent = `${cardIndex + 1} / ${s.flashcards.length}`;

      const jump = buildJumpDropdown(
        () => s.flashcards.map((fc, i) => `Card ${i + 1}: ${fc.front.slice(0, 30)}${fc.front.length > 30 ? "…" : ""}`),
        () => cardIndex,
        (i) => { cardIndex = i; render(); }
      );

      topRow.appendChild(counter);
      topRow.appendChild(jump);
      panel.appendChild(topRow);

      const cardEl = document.createElement("div");
      cardEl.className = "flashcard";

      const front = document.createElement("div");
      front.className = "flashcard-face flashcard-front";
      front.textContent = card.front;

      const back = document.createElement("div");
      back.className = "flashcard-face flashcard-back hidden";
      back.textContent = card.back;

      // ── Star button — works whether flipped or not ──
      const itemId = card._savedId || makeItemId("flashcard", concept.concept_id, cardIndex);
      const star = buildStarButton(itemId);
      star.classList.add("flashcard-star"); // for any flashcard-specific star tweaks

      const flipBtn = document.createElement("button");
      flipBtn.className = "card-btn";
      flipBtn.textContent = "Flip";
      flipBtn.onclick = () => {
        flipped = !flipped;
        front.classList.toggle("hidden", flipped);
        back.classList.toggle("hidden", !flipped);
        flipBtn.textContent = flipped ? "Show Question" : "Flip";
      };

      const nav = document.createElement("div");
      nav.className = "card-nav";

      const prevBtn = document.createElement("button");
      prevBtn.className = "card-btn";
      prevBtn.textContent = "← Prev";
      prevBtn.disabled = cardIndex === 0;
      prevBtn.onclick = () => { cardIndex--; render(); };

      const nextBtn = document.createElement("button");
      nextBtn.className = "card-btn";
      nextBtn.textContent = "Next →";
      nextBtn.disabled = cardIndex === s.flashcards.length - 1;
      nextBtn.onclick = () => { cardIndex++; render(); };

      nav.appendChild(prevBtn);
      nav.appendChild(flipBtn);
      nav.appendChild(nextBtn);

      cardEl.appendChild(front);
      cardEl.appendChild(back);
      cardEl.appendChild(star)

      // click handler
      cardEl.style.cursor = "pointer";
      cardEl.onclick = (e) => {
        if(e.target === star) return; // don't flip when clicking star
        flipped = !flipped;
        front.classList.toggle("hidden", flipped);
        back.classList.toggle("hidden", !flipped);
        flipBtn.textContent = flipped ? "Show Question" : "Flip";
      }
      
      panel.appendChild(cardEl);
      panel.appendChild(nav);
    };

    render();
  }

  if (mode === "quiz") {
    if (!s.quiz_questions?.length) {
      panel.innerHTML = `<p class="empty-state">No quiz questions for this concept.</p>`;
      return;
    }

    const conceptId = concept.concept_id;
    const state = getQuizState(conceptId);
    let qIndex = 0;

    if (state.completed) {
      renderQuizResults(concept, panel, state);
      return;
    }

    const renderQ = () => {
      panel.innerHTML = "";
      const q = s.quiz_questions[qIndex];

      // ── Header ──
      const header = document.createElement("div");
      header.className = "quiz-header";

      const counter = document.createElement("div");
      counter.className = "card-counter";
      counter.textContent = `Question ${qIndex + 1} / ${s.quiz_questions.length}`;

      const jump = buildJumpDropdown(
        () => s.quiz_questions.map((q, i) => `Q${i + 1}: ${q.question.slice(0, 28)}${q.question.length > 28 ? "…" : ""}`),
        () => qIndex,
        (i) => { qIndex = i; renderQ(); }
      );

      header.appendChild(counter);
      header.appendChild(jump);
      panel.appendChild(header);

      // ── Question row ──
      const qRow = document.createElement("div");
      qRow.className = "quiz-question-row";

      const qText = document.createElement("p");
      qText.className = "quiz-question";
      qText.textContent = q.question;

      const itemId = q._savedId || makeItemId("quiz", conceptId, qIndex);
      const bookmark = buildBannerButton(itemId);
      qRow.appendChild(qText);
      qRow.appendChild(bookmark);
      panel.appendChild(qRow);

      // ── Submit row — defined early so updateSubmitBtn is available ──
      const submitRow = document.createElement("div");
      submitRow.className = "quiz-submit-row";

      const submitBtn = document.createElement("button");
      submitBtn.className = "quiz-submit-btn";
      submitBtn.textContent = "Submit Quiz";
      submitBtn.onclick = () => {
        state.completed = true;
        submitQuizBatch(concept, s.quiz_questions, state);
        renderQuizResults(concept, panel, state);
      };
      submitRow.appendChild(submitBtn);

      const updateSubmitBtn = () => {
        const allAnswered = state.answers.size === s.quiz_questions.length;
        submitRow.classList.toggle("hidden", !allAnswered);
      };

      // ── Answer controls ──
      const savedAnswer = state.answers.get(qIndex);

      const handleSelect = (chosen, btn, allBtns) => {
        // Update state
        state.answers.set(qIndex, chosen);

        // Update button visual — just "selected", no correct/wrong yet
        allBtns.forEach(b => b.classList.remove("choice-selected"));
        btn.classList.add("choice-selected");

        updateSubmitBtn();
      };

      const buildChoiceButtons = (choices) => {
        const btns = [];
        choices.forEach(choice => {
          const btn = document.createElement("button");
          btn.className = "choice-btn";
          btn.textContent = choice;
          btn.dataset.choice = choice;

          // Restore selected state if already answered
          if (choice === savedAnswer) {
            btn.classList.add("choice-selected");
          }

          btn.onclick = () => handleSelect(choice, btn, btns);
          panel.appendChild(btn);
          btns.push(btn);
        });
      };

      if (q.type === "multiple_choice" && q.choices?.length) {
        buildChoiceButtons(q.choices);
      } else if (q.type === "true_false") {
        buildChoiceButtons(["True", "False"]);
      } else {
        // Short answer — text input
        const inputWrapper = document.createElement("div");
        inputWrapper.className = "short-answer-wrapper";

        const input = document.createElement("textarea");
        input.className = "short-answer-input";
        input.placeholder = "Type your answer here...";
        input.rows = 3;

        // Restore saved answer if exists
        if (savedAnswer !== undefined) {
          input.value = savedAnswer;
        }

        // Save answer to state on every keystroke
        input.addEventListener("input", () => {
          const val = input.value.trim();
          if (val) {
            state.answers.set(qIndex, input.value); // store raw value
          } else {
            state.answers.delete(qIndex);
          }
          updateSubmitBtn();
        });

        inputWrapper.appendChild(input);
        panel.appendChild(inputWrapper);
      }

      // ── Nav ──
      const nav = document.createElement("div");
      nav.className = "card-nav";

      const prevBtn = document.createElement("button");
      prevBtn.className = "card-btn";
      prevBtn.textContent = "← Prev";
      prevBtn.disabled = qIndex === 0;
      prevBtn.onclick = () => { qIndex--; renderQ(); };

      const nextBtn = document.createElement("button");
      nextBtn.className = "card-btn";
      nextBtn.textContent = "Next →";
      nextBtn.disabled = qIndex === s.quiz_questions.length - 1;
      nextBtn.onclick = () => { qIndex++; renderQ(); };

      nav.appendChild(prevBtn);
      nav.appendChild(nextBtn);
      panel.appendChild(nav);
      panel.appendChild(submitRow);

      updateSubmitBtn();
    };

    renderQ();
  }
}

function buildJumpDropdown(getLabels, getCurrent, onSelect) {
  const wrap = document.createElement("div");
  wrap.className = "jump-dropdown-wrap";

  const btn = document.createElement("button");
  btn.className = "jump-btn";
  btn.textContent = "↓ Jump";

  const menu = document.createElement("div");
  menu.className = "jump-menu";

  btn.onclick = (e) => {
    e.stopPropagation();
    const isOpen = menu.classList.toggle("open");
    if (isOpen) {
      // rebuild items each open so labels/active state are fresh
      menu.innerHTML = "";
      getLabels().forEach((label, i) => {
        const item = document.createElement("button");
        item.className = "jump-item" + (i === getCurrent() ? " jump-item-active" : "");
        item.textContent = label;
        item.onclick = (e) => {
          e.stopPropagation();
          menu.classList.remove("open");
          onSelect(i);
        };
        menu.appendChild(item);
      });
      // scroll active item into view
      const active = menu.querySelector(".jump-item-active");
      if (active) active.scrollIntoView({ block: "nearest" });
    }
  };

  // close on outside click
  document.addEventListener("click", () => menu.classList.remove("open"));

  wrap.appendChild(btn);
  wrap.appendChild(menu);
  return wrap;
}

function buildStarButton(itemId) {
  const star = document.createElement("button");
  star.className = "star-btn" + (isSaved(itemId) ? " star-active" : "");
  star.innerHTML = isSaved(itemId) ? "★" : "☆";
  star.setAttribute("aria-label", "Save this item");
  star.onclick = (e) => {
    e.stopPropagation();
    toggleSaved(itemId);
    star.classList.toggle("star-active");
    star.innerHTML = isSaved(itemId) ? "★" : "☆";
  };
  return star;
}

function buildBannerButton(itemId) {
  const BOOKMARK_OUTLINE = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>`;
  const BOOKMARK_FILLED  = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>`;
  
  const saved = isSaved(itemId);
  const btn = document.createElement("button");
  btn.className = "banner-btn" + (saved ? " banner-active" : "");
  btn.innerHTML = saved ? BOOKMARK_FILLED : BOOKMARK_OUTLINE;
  btn.setAttribute("aria-label", "Save this question");
  btn.onclick = (e) => {
    e.stopPropagation();
    toggleSaved(itemId);
    const nowSaved = isSaved(itemId);
    btn.classList.toggle("banner-active", nowSaved);
    btn.innerHTML = nowSaved ? BOOKMARK_FILLED : BOOKMARK_OUTLINE;
  };
  return btn;
}

// ─── Quiz State ───────────────────────────────────────────────────────────────
// Persists answers across concept navigation
// Key: concept_id, Value: { answers: Map(qIndex → chosenAnswer), completed: bool }
const quizState = new Map();

function getQuizState(conceptId) {
  if (!quizState.has(conceptId)) {
    quizState.set(conceptId, { answers: new Map(), completed: false });
  }
  return quizState.get(conceptId);
}

function resetQuizState(conceptId) {
  quizState.set(conceptId, { answers: new Map(), completed: false });
}

function sectionHeading(text) {
  const h = document.createElement("h3");
  h.className = "study-section-heading";
  h.textContent = text;
  return h;
}

function submitQuizBatch(concept, questions, state) {
  const attempts = [];
  questions.forEach((q, i) => {
    if (q.type === "short_answer") return;
    attempts.push({ question_index: i, answer_given: state.answers.get(i) ?? null });
  });
  if (!attempts.length) return;

  fetch(`/api/lessons/${lessonId}/quiz-attempt-batch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ concept_id: concept.concept_id, attempts })
  }).catch(err => console.warn("Could not save quiz attempt:", err.message));
}

function renderQuizResults(concept, panel, state) {
  const s = concept.study;
  const questions = s.quiz_questions;
  panel.innerHTML = "";

  // Score
  let correct = 0;
  let gradeable = 0;
  questions.forEach((q, i) => {
    if (q.type === "short_answer") return;
    gradeable++;
    const chosen = state.answers.get(i);
    if (chosen?.toLowerCase() === q.answer.toLowerCase()) correct++;
  });

  const pct = gradeable > 0 ? Math.round((correct / gradeable) * 100) : null;

  const heading = document.createElement("h3");
  heading.className = "study-section-heading";
  heading.textContent = "Quiz Complete";
  panel.appendChild(heading);

  const scoreCard = document.createElement("div");
  scoreCard.className = "quiz-results-score";
  if (pct !== null) {
    const scoreNum = document.createElement("div");
    scoreNum.className = "quiz-results-number";
    scoreNum.textContent = `${pct}%`;
    const scoreLabel = document.createElement("div");
    scoreLabel.className = "quiz-results-label";
    scoreLabel.textContent = `${correct} / ${gradeable} correct`;
    scoreCard.appendChild(scoreNum);
    scoreCard.appendChild(scoreLabel);
  } else {
    scoreCard.textContent = "All questions revealed — review your answers below.";
  }
  panel.appendChild(scoreCard);

  // Per-question breakdown with full feedback
  const breakdown = document.createElement("div");
  breakdown.className = "quiz-results-breakdown";

  questions.forEach((q, i) => {
    const chosen = state.answers.get(i);
    const isShortAnswer = q.type === "short_answer";
    const isCorrect = !isShortAnswer && chosen?.toLowerCase() === q.answer.toLowerCase();

    const block = document.createElement("div");
    block.className = `quiz-results-row ${isShortAnswer ? "results-neutral" : isCorrect ? "results-correct" : "results-wrong"}`;

    const icon = document.createElement("span");
    icon.className = "results-icon";
    icon.textContent = isShortAnswer ? "—" : isCorrect ? "✓" : "✗";

    const content = document.createElement("div");
    content.className = "results-content";

    const qText = document.createElement("div");
    qText.className = "results-question";
    qText.textContent = `${i + 1}. ${q.question}`;
    content.appendChild(qText);

    if (isShortAnswer) {
      const yourAnswer = document.createElement("div");
      yourAnswer.className = "results-your-answer results-your-answer-neutral"; 
      yourAnswer.textContent = `Your answer: "${chosen || ""}"`;
      content.appendChild(yourAnswer);

      const answerEl = document.createElement("div");
      answerEl.className = "results-correct-answer";
      answerEl.textContent = `Answer: "${q.answer}"`;
      content.appendChild(answerEl);
    } else {
      // Show chosen answer if wrong
      if (!isCorrect && chosen) {
        const yourAnswer = document.createElement("div");
        yourAnswer.className = "results-your-answer";
        yourAnswer.textContent = `Your answer: ${chosen}`;
        content.appendChild(yourAnswer);
      }
      // Show correct answer if wrong
      if (!isCorrect) {
        const correctAnswer = document.createElement("div");
        correctAnswer.className = "results-correct-answer";
        correctAnswer.textContent = `Correct: ${q.answer}`;
        content.appendChild(correctAnswer);
      }
      // Show explanation
      if (q.explanation) {
        const explanation = document.createElement("div");
        explanation.className = "results-explanation";
        explanation.textContent = q.explanation;
        content.appendChild(explanation);
      }
    }

    block.appendChild(icon);
    block.appendChild(content);
    breakdown.appendChild(block);
  });

  panel.appendChild(breakdown);

  const retryBtn = document.createElement("button");
  retryBtn.className = "card-btn";
  retryBtn.textContent = "Retry Quiz";
  retryBtn.onclick = () => {
    resetQuizState(concept.concept_id);
    renderStudyPanel(concept, "quiz", panel);
  };
  panel.appendChild(retryBtn);
}