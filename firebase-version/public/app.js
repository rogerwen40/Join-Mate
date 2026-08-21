import { initializeApp } from "https://www.gstatic.com/firebasejs/12.17.1/firebase-app.js";
import {
  getAuth,
  GoogleAuthProvider,
  onAuthStateChanged,
  signInWithPopup,
  signOut
} from "https://www.gstatic.com/firebasejs/12.17.1/firebase-auth.js";
import {
  Timestamp,
  collection,
  doc,
  getDoc,
  getDocs,
  getFirestore,
  query,
  serverTimestamp,
  setDoc,
  updateDoc,
  where,
  writeBatch
} from "https://www.gstatic.com/firebasejs/12.17.1/firebase-firestore.js";
import { firebaseConfig } from "./firebase-config.js?v=3";
import { joinMateMailerUrl } from "./mailer-config.js?v=2";

const configured = firebaseConfig.apiKey && !firebaseConfig.apiKey.startsWith("PASTE_");
const setupWarning = document.querySelector("#setup-warning");
const messageBox = document.querySelector("#message");
const loginButton = document.querySelector("#login-button");
const logoutButton = document.querySelector("#logout-button");
const userMenu = document.querySelector("#user-menu");
const userName = document.querySelector("#user-name");
const userPhoto = document.querySelector("#user-photo");
const activityGrid = document.querySelector("#activity-grid");
const emptyState = document.querySelector("#empty-state");
const mineGrid = document.querySelector("#mine-grid");
const mineEmpty = document.querySelector("#mine-empty");
const detailContainer = document.querySelector("#activity-detail");
const inviteDialog = document.querySelector("#invite-dialog");
const editActivityForm = document.querySelector("#edit-activity-form");

let auth;
let db;
let currentUser = null;
let publicActivities = [];
let currentActivity = null;
let currentRegistrations = [];

if (!configured) {
  setupWarning.hidden = false;
  loginButton.disabled = true;
  document.querySelector("#activity-count").textContent = "等待 Firebase 設定";
} else {
  const app = initializeApp(firebaseConfig);
  auth = getAuth(app);
  db = getFirestore(app);
  onAuthStateChanged(auth, handleAuthState);
}

function showMessage(text, type = "success") {
  messageBox.textContent = text;
  messageBox.className = `message ${type}`;
  messageBox.hidden = false;
  window.clearTimeout(showMessage.timer);
  showMessage.timer = window.setTimeout(() => { messageBox.hidden = true; }, 5000);
}

function friendlyError(error) {
  console.error(error);
  const code = error?.code || "";
  if (code.includes("permission-denied")) return "你沒有權限執行這個操作，或邀請碼已失效。";
  if (code.includes("popup-closed")) return "登入視窗已關閉，尚未完成登入。";
  if (code.includes("network")) return "網路連線失敗，請稍後再試。";
  return error?.message || "發生未預期的錯誤，請稍後再試。";
}

function requireUser() {
  if (currentUser) return true;
  showMessage("請先使用 Google 帳號登入。", "error");
  return false;
}

async function sendFirebaseMail(action, activityId) {
  if (!joinMateMailerUrl || !currentUser) return false;
  const idToken = await currentUser.getIdToken();
  await fetch(joinMateMailerUrl, {
    method: "POST",
    mode: "no-cors",
    headers: { "Content-Type": "text/plain;charset=utf-8" },
    body: JSON.stringify({ action, activityId, idToken })
  });
  return true;
}

async function handleAuthState(user) {
  currentUser = user;
  document.querySelectorAll(".signed-in-only").forEach((element) => { element.hidden = !user; });
  loginButton.hidden = Boolean(user);
  userMenu.hidden = !user;
  if (user) {
    userName.textContent = user.displayName || user.email;
    userPhoto.src = user.photoURL || "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E";
    await setDoc(doc(db, "users", user.uid), {
      displayName: user.displayName || user.email?.split("@")[0] || "JoinMate 成員",
      email: user.email || "",
      photoURL: user.photoURL || "",
      updatedAt: serverTimestamp()
    }, { merge: true });
  }
  await loadPublicActivities();
  routeFromHash();
}

loginButton.addEventListener("click", async () => {
  if (!configured) return;
  try {
    await signInWithPopup(auth, new GoogleAuthProvider());
  } catch (error) {
    showMessage(friendlyError(error), "error");
  }
});

logoutButton.addEventListener("click", async () => {
  await signOut(auth);
  location.hash = "home";
});

function showView(name) {
  document.querySelectorAll(".view").forEach((view) => { view.hidden = true; });
  const target = document.querySelector(`#${name}-view`);
  if (target) target.hidden = false;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function navigate(route) {
  if ((route === "new" || route === "mine") && !requireUser()) return;
  location.hash = route;
}

document.addEventListener("click", (event) => {
  const routeButton = event.target.closest("[data-route]");
  if (routeButton) navigate(routeButton.dataset.route);

  const actionButton = event.target.closest("[data-action]");
  if (!actionButton) return;
  const action = actionButton.dataset.action;
  if (action === "open-invite") inviteDialog.showModal();
  if (action === "close-invite") inviteDialog.close();
  if (action === "register") registerForCurrentActivity();
  if (action === "cancel-registration") cancelCurrentRegistration();
  if (action === "cancel-activity") cancelCurrentActivity();
  if (action === "edit-activity") navigate(`edit/${currentActivity.id}`);
  if (action === "copy-share") copyShareText();
  if (action === "line-share") shareToLine();
});

window.addEventListener("hashchange", routeFromHash);

async function routeFromHash() {
  const route = location.hash.replace(/^#/, "") || "home";
  if (route.startsWith("activity/")) {
    const activityId = route.slice("activity/".length);
    await openActivity(activityId);
    return;
  }
  if (route.startsWith("edit/")) {
    const activityId = route.slice("edit/".length);
    await openEditActivity(activityId);
    return;
  }
  if (route === "new") {
    if (!requireUser()) return navigate("home");
    showView("new");
    setDefaultTimes();
    return;
  }
  if (route === "mine") {
    if (!requireUser()) return navigate("home");
    showView("mine");
    await loadMyActivities();
    return;
  }
  showView("home");
}

function setDefaultTimes() {
  const form = document.querySelector("#activity-form");
  if (form.elements.startsAt.value) return;
  const start = new Date(Date.now() + 24 * 60 * 60 * 1000);
  start.setMinutes(0, 0, 0);
  const end = new Date(start.getTime() + 2 * 60 * 60 * 1000);
  form.elements.startsAt.value = toLocalInput(start);
  form.elements.endsAt.value = toLocalInput(end);
}

function toLocalInput(date) {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

async function loadPublicActivities() {
  if (!configured) return;
  const count = document.querySelector("#activity-count");
  count.textContent = "載入中…";
  try {
    const snapshot = await getDocs(query(collection(db, "activities"), where("visibility", "==", "public")));
    publicActivities = snapshot.docs.map((item) => ({ id: item.id, ...item.data() }));
    publicActivities.sort((a, b) => toDate(a.startsAt) - toDate(b.startsAt));
    populateTypeFilter();
    renderPublicActivities();
  } catch (error) {
    count.textContent = "載入失敗";
    showMessage(friendlyError(error), "error");
  }
}

function populateTypeFilter() {
  const select = document.querySelector("#filter-type");
  const current = select.value;
  const types = [...new Set(publicActivities.map((activity) => activity.activityType).filter(Boolean))].sort();
  select.innerHTML = '<option value="">全部類型</option>' + types.map((type) => `<option value="${escapeHtml(type)}">${escapeHtml(type)}</option>`).join("");
  select.value = current;
}

function renderPublicActivities() {
  const queryText = document.querySelector("#filter-query").value.trim().toLowerCase();
  const type = document.querySelector("#filter-type").value;
  const date = document.querySelector("#filter-date").value;
  const availableOnly = document.querySelector("#filter-available").checked;
  const now = new Date();
  const filtered = publicActivities.filter((activity) => {
    const haystack = `${activity.title} ${activity.location} ${activity.activityType}`.toLowerCase();
    const startsAt = toDate(activity.startsAt);
    return (!queryText || haystack.includes(queryText))
      && (!type || activity.activityType === type)
      && (!date || localDateKey(startsAt) === date)
      && (!availableOnly || (activity.status === "open" && startsAt > now));
  });
  activityGrid.innerHTML = filtered.map(activityCard).join("");
  activityGrid.querySelectorAll(".activity-card").forEach((card) => {
    card.addEventListener("click", () => navigate(`activity/${card.dataset.id}`));
  });
  emptyState.hidden = filtered.length > 0;
  document.querySelector("#activity-count").textContent = `${filtered.length} 個活動`;
}

document.querySelector("#filters").addEventListener("input", renderPublicActivities);
document.querySelector("#filters").addEventListener("reset", () => window.setTimeout(renderPublicActivities));
document.querySelector("#refresh-button").addEventListener("click", loadPublicActivities);

function activityCard(activity) {
  const startsAt = toDate(activity.startsAt);
  const status = activity.status === "cancelled" ? "已取消" : (startsAt < new Date() ? "已結束" : "開放報名");
  return `<article class="activity-card" data-id="${activity.id}">
    <div class="card-topline"><span class="pill">${escapeHtml(activity.activityType)}</span><span class="pill status">${status}</span></div>
    <h3>${activityEmoji(activity)} ${escapeHtml(activity.title)}</h3>
    <div class="activity-meta">
      <p>📅 ${formatDate(startsAt)}${activity.endsAt ? `～${formatTime(toDate(activity.endsAt))}` : ""}</p>
      <p>📍 ${escapeHtml(activity.location)}</p>
      <p>👥 正取上限 ${Number(activity.maxPeople || 0)} 人</p>
    </div>
  </article>`;
}

document.querySelector("#activity-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!requireUser()) return;
  const form = event.currentTarget;
  const values = new FormData(form);
  const startsAt = new Date(values.get("startsAt"));
  const endsAt = new Date(values.get("endsAt"));
  const minPeople = Number(values.get("minPeople"));
  const maxPeople = Number(values.get("maxPeople"));
  if (endsAt <= startsAt) return showMessage("結束時間必須晚於開始時間。", "error");
  if (maxPeople < minPeople) return showMessage("正取上限不能小於最低成團人數。", "error");

  const createButton = document.querySelector("#create-button");
  createButton.disabled = true;
  createButton.textContent = "建立中…";
  try {
    const activityRef = doc(collection(db, "activities"));
    const visibility = values.get("visibility");
    const inviteCode = visibility === "invite" ? randomInviteCode() : "";
    const data = {
      title: values.get("title").trim(),
      activityType: values.get("activityType").trim(),
      location: values.get("location").trim(),
      startsAt: Timestamp.fromDate(startsAt),
      endsAt: Timestamp.fromDate(endsAt),
      minPeople,
      maxPeople,
      fee: Number(values.get("fee")),
      description: values.get("description").trim(),
      visibility,
      inviteCode,
      creatorId: currentUser.uid,
      creatorName: currentUser.displayName || currentUser.email,
      editorEmails: [],
      status: "open",
      createdAt: serverTimestamp(),
      updatedAt: serverTimestamp()
    };
    const batch = writeBatch(db);
    batch.set(activityRef, data);
    batch.set(doc(db, "users", currentUser.uid, "createdActivities", activityRef.id), {
      activityId: activityRef.id,
      createdAt: serverTimestamp()
    });
    if (inviteCode) {
      batch.set(doc(db, "invites", inviteCode), {
        activityId: activityRef.id,
        creatorId: currentUser.uid,
        createdAt: serverTimestamp()
      });
      batch.set(doc(db, "users", currentUser.uid, "activityAccess", activityRef.id), {
        inviteCode,
        grantedAt: serverTimestamp()
      });
    }
    await batch.commit();
    form.reset();
    showMessage(inviteCode ? `活動已建立，邀請碼是 ${inviteCode}` : "公開活動已建立。", "success");
    await loadPublicActivities();
    navigate(`activity/${activityRef.id}`);
  } catch (error) {
    showMessage(friendlyError(error), "error");
  } finally {
    createButton.disabled = false;
    createButton.textContent = "建立活動";
  }
});

document.querySelector("#invite-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!requireUser()) return;
  const code = document.querySelector("#invite-code").value.trim().toUpperCase();
  if (!code) return;
  try {
    const inviteSnapshot = await getDoc(doc(db, "invites", code));
    if (!inviteSnapshot.exists()) throw new Error("找不到這組邀請碼。請確認是否輸入正確。");
    const activityId = inviteSnapshot.data().activityId;
    await setDoc(doc(db, "users", currentUser.uid, "activityAccess", activityId), {
      inviteCode: code,
      grantedAt: serverTimestamp()
    });
    inviteDialog.close();
    document.querySelector("#invite-code").value = "";
    navigate(`activity/${activityId}`);
  } catch (error) {
    showMessage(friendlyError(error), "error");
  }
});

async function openActivity(activityId) {
  if (!configured) return showView("detail");
  try {
    const snapshot = await getDoc(doc(db, "activities", activityId));
    if (!snapshot.exists()) throw new Error("找不到這個活動，可能已被刪除。");
    currentActivity = { id: snapshot.id, ...snapshot.data() };
    currentRegistrations = [];
    if (currentUser) {
      const registrationSnapshot = await getDocs(collection(db, "activities", activityId, "registrations"));
      currentRegistrations = registrationSnapshot.docs.map((item) => ({ id: item.id, ...item.data() }));
    }
    renderActivityDetail();
    if (canEditActivity(currentActivity)) {
      void sendFirebaseMail("firebase_sync_reminders", currentActivity.id)
        .catch((error) => console.error("Reminder sync failed", error));
    }
    showView("detail");
  } catch (error) {
    showMessage(friendlyError(error), "error");
    navigate("home");
  }
}

function renderActivityDetail() {
  const activity = currentActivity;
  const startsAt = toDate(activity.startsAt);
  const endsAt = activity.endsAt ? toDate(activity.endsAt) : null;
  const registered = currentRegistrations.filter((item) => item.status === "registered");
  const waitlisted = currentRegistrations.filter((item) => item.status === "waitlisted");
  const mine = currentUser ? currentRegistrations.find((item) => item.userId === currentUser.uid) : null;
  const isOwner = currentUser?.uid === activity.creatorId;
  const isEditor = canEditActivity(activity);
  const canRegister = activity.status === "open" && startsAt > new Date();
  const statusText = activity.status === "cancelled"
    ? "活動已取消"
    : (!currentUser
      ? "登入後查看名單"
      : (registered.length >= activity.minPeople ? "已成團" : `再 ${Math.max(0, activity.minPeople - registered.length)} 人成團`));
  const peopleText = currentUser
    ? `正取 ${registered.length}/${activity.maxPeople} 人・最低 ${activity.minPeople} 人成團`
    : `正取上限 ${activity.maxPeople} 人・最低 ${activity.minPeople} 人成團`;

  detailContainer.innerHTML = `
    <div class="card-topline"><span class="pill">${escapeHtml(activity.activityType)}</span><span class="pill status">${statusText}</span></div>
    <h1>${activityEmoji(activity)} ${escapeHtml(activity.title)}</h1>
    ${activity.visibility === "invite" && isOwner ? `<div class="invite-box">私人活動邀請碼：<code>${escapeHtml(activity.inviteCode)}</code></div>` : ""}
    <div class="detail-grid">
      <div><small>時間</small><strong>${formatDate(startsAt)}${endsAt ? `～${formatTime(endsAt)}` : ""}</strong></div>
      <div><small>地點</small><strong>${escapeHtml(activity.location)}</strong></div>
      <div><small>人數</small><strong>${peopleText}</strong></div>
      <div><small>費用</small><strong>${activity.fee ? `NT$ ${Number(activity.fee).toLocaleString()}` : "免費"}</strong></div>
      <div><small>建立者</small><strong>${escapeHtml(activity.creatorName || "JoinMate 成員")}</strong></div>
      <div><small>隱私</small><strong>${activity.visibility === "invite" ? "邀請碼活動" : "公開活動"}</strong></div>
    </div>
    ${activity.editorEmails?.length ? `<div class="invite-box"><strong>共同管理者</strong><br>${activity.editorEmails.map(escapeHtml).join("、")}</div>` : ""}
    <div class="description">${escapeHtml(activity.description || "沒有其他活動說明。")}</div>
    <div class="action-row">
      ${!currentUser ? '<button class="primary-button" id="detail-login">登入後報名</button>' : ""}
      ${currentUser && !mine && canRegister ? '<button class="primary-button" data-action="register">我要報名</button>' : ""}
      ${mine ? `<button class="danger-button" data-action="cancel-registration">取消${mine.status === "registered" ? "報名" : "候補"}</button>` : ""}
      ${isEditor ? '<button class="primary-button" data-action="edit-activity">編輯活動</button>' : ""}
      ${isOwner && activity.status === "open" ? '<button class="danger-button" data-action="cancel-activity">取消整個活動</button>' : ""}
      <button class="secondary-button" data-action="copy-share">複製分享內容</button>
      <button class="secondary-button" data-action="line-share">LINE 分享</button>
    </div>
    ${mine ? `<p class="message success">你的狀態：${mine.status === "registered" ? "正取" : "候補"}</p>` : ""}
    <section class="roster">
      ${currentUser ? `<h2>正取名單（${registered.length}）</h2>
        <div class="roster-list">${registered.length ? registered.map(rosterItem).join("") : '<p class="muted">還沒有人報名。</p>'}</div>
        ${waitlisted.length ? `<h2>候補名單（${waitlisted.length}）</h2><div class="roster-list">${waitlisted.map(rosterItem).join("")}</div>` : ""}` : '<p class="muted">登入後即可查看活動名單與報名。</p>'}
    </section>`;
  const detailLogin = document.querySelector("#detail-login");
  if (detailLogin) detailLogin.addEventListener("click", () => loginButton.click());
}

function canEditActivity(activity) {
  if (!currentUser || !activity) return false;
  const email = (currentUser.email || "").toLowerCase();
  return activity.creatorId === currentUser.uid
    || (activity.editorEmails || []).map((item) => String(item).toLowerCase()).includes(email);
}

async function openEditActivity(activityId) {
  if (!requireUser()) return navigate("home");
  try {
    const snapshot = await getDoc(doc(db, "activities", activityId));
    if (!snapshot.exists()) throw new Error("找不到這個活動。");
    currentActivity = { id: snapshot.id, ...snapshot.data() };
    if (!canEditActivity(currentActivity)) throw new Error("只有建立者或共同管理者可以編輯活動。");
    const form = editActivityForm;
    if (!form) {
      throw new Error("編輯表單載入失敗，請按 Ctrl + F5 重新整理後再試。");
    }
    form.elements.title.value = currentActivity.title || "";
    form.elements.activityType.value = currentActivity.activityType || "";
    form.elements.location.value = currentActivity.location || "";
    form.elements.startsAt.value = toLocalInput(toDate(currentActivity.startsAt));
    form.elements.endsAt.value = toLocalInput(toDate(currentActivity.endsAt));
    form.elements.minPeople.value = currentActivity.minPeople || 2;
    form.elements.maxPeople.value = currentActivity.maxPeople || 10;
    form.elements.fee.value = currentActivity.fee || 0;
    form.elements.description.value = currentActivity.description || "";
    form.elements.editorEmails.value = (currentActivity.editorEmails || []).join("\n");
    document.querySelector("#co-organizer-field").hidden = currentActivity.creatorId !== currentUser.uid;
    document.querySelector("#edit-back-button").onclick = () => navigate(`activity/${activityId}`);
    showView("edit");
  } catch (error) {
    showMessage(friendlyError(error), "error");
    navigate(`activity/${activityId}`);
  }
}

editActivityForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentActivity || !canEditActivity(currentActivity)) return;
  const form = event.currentTarget;
  const values = new FormData(form);
  const startsAt = new Date(values.get("startsAt"));
  const endsAt = new Date(values.get("endsAt"));
  const minPeople = Number(values.get("minPeople"));
  const maxPeople = Number(values.get("maxPeople"));
  if (endsAt <= startsAt) return showMessage("結束時間必須晚於開始時間。", "error");
  if (maxPeople < minPeople) return showMessage("正取上限不能小於最低成團人數。", "error");

  const updates = {
    title: values.get("title").trim(),
    activityType: values.get("activityType").trim(),
    location: values.get("location").trim(),
    startsAt: Timestamp.fromDate(startsAt),
    endsAt: Timestamp.fromDate(endsAt),
    minPeople,
    maxPeople,
    fee: Number(values.get("fee")),
    description: values.get("description").trim(),
    updatedAt: serverTimestamp()
  };
  if (currentActivity.creatorId === currentUser.uid) {
    updates.editorEmails = [...new Set(values.get("editorEmails")
      .split(/[\n,;]+/)
      .map((email) => email.trim().toLowerCase())
      .filter((email) => email && email !== (currentUser.email || "").toLowerCase()))];
  }

  const saveButton = document.querySelector("#save-activity-button");
  saveButton.disabled = true;
  saveButton.textContent = "儲存中…";
  try {
    await updateDoc(doc(db, "activities", currentActivity.id), updates);
    void sendFirebaseMail("firebase_activity_changed", currentActivity.id)
      .catch((error) => console.error("Activity email failed", error));
    showMessage("活動內容已更新。", "success");
    await loadPublicActivities();
    navigate(`activity/${currentActivity.id}`);
  } catch (error) {
    showMessage(friendlyError(error), "error");
  } finally {
    saveButton.disabled = false;
    saveButton.textContent = "儲存修改";
  }
});

function rosterItem(registration, index) {
  return `<div class="roster-item"><span>${index + 1}. ${escapeHtml(registration.displayName)}</span><small>${registration.status === "registered" ? "正取" : "候補"}</small></div>`;
}

async function registerForCurrentActivity() {
  if (!requireUser() || !currentActivity) return;
  try {
    const latestActivity = await getDoc(doc(db, "activities", currentActivity.id));
    if (!latestActivity.exists() || latestActivity.data().status !== "open") throw new Error("這個活動目前不能報名。");
    const registrations = await getDocs(collection(db, "activities", currentActivity.id, "registrations"));
    const rows = registrations.docs.map((item) => item.data());
    const registeredCount = rows.filter((item) => item.status === "registered").length;
    const status = registeredCount < Number(latestActivity.data().maxPeople) ? "registered" : "waitlisted";
    const batch = writeBatch(db);
    batch.set(doc(db, "activities", currentActivity.id, "registrations", currentUser.uid), {
      userId: currentUser.uid,
      displayName: currentUser.displayName || currentUser.email,
      email: currentUser.email || "",
      status,
      createdAt: serverTimestamp()
    });
    batch.set(doc(db, "users", currentUser.uid, "registrations", currentActivity.id), {
      activityId: currentActivity.id,
      status,
      createdAt: serverTimestamp()
    });
    await batch.commit();
    void sendFirebaseMail("firebase_registration", currentActivity.id)
      .catch((error) => console.error("Registration email failed", error));
    showMessage(status === "registered" ? "報名成功，你是正取。" : "正取已滿，已加入候補。", "success");
    await openActivity(currentActivity.id);
  } catch (error) {
    showMessage(friendlyError(error), "error");
  }
}

async function cancelCurrentRegistration() {
  if (!requireUser() || !currentActivity) return;
  if (!confirm("確定要取消這次報名嗎？")) return;
  try {
    const batch = writeBatch(db);
    batch.delete(doc(db, "activities", currentActivity.id, "registrations", currentUser.uid));
    batch.delete(doc(db, "users", currentUser.uid, "registrations", currentActivity.id));
    await batch.commit();
    void sendFirebaseMail("firebase_registration_cancelled", currentActivity.id)
      .catch((error) => console.error("Registration cancellation email failed", error));
    showMessage("已取消報名。", "success");
    await openActivity(currentActivity.id);
  } catch (error) {
    showMessage(friendlyError(error), "error");
  }
}

async function cancelCurrentActivity() {
  if (!requireUser() || !currentActivity) return;
  if (!confirm("確定要取消整個活動嗎？參加者將無法再報名。")) return;
  try {
    await updateDoc(doc(db, "activities", currentActivity.id), {
      status: "cancelled",
      updatedAt: serverTimestamp()
    });
    void sendFirebaseMail("firebase_activity_changed", currentActivity.id)
      .catch((error) => console.error("Cancellation email failed", error));
    showMessage("活動已取消。", "success");
    await loadPublicActivities();
    await openActivity(currentActivity.id);
  } catch (error) {
    showMessage(friendlyError(error), "error");
  }
}

async function loadMyActivities() {
  if (!currentUser) return;
  mineGrid.innerHTML = "";
  try {
    const ids = new Set();
    const [createdSnapshot, registeredSnapshot, accessSnapshot] = await Promise.all([
      getDocs(collection(db, "users", currentUser.uid, "createdActivities")),
      getDocs(collection(db, "users", currentUser.uid, "registrations")),
      getDocs(collection(db, "users", currentUser.uid, "activityAccess"))
    ]);
    [...createdSnapshot.docs, ...registeredSnapshot.docs, ...accessSnapshot.docs].forEach((item) => ids.add(item.data().activityId || item.id));
    const editorSnapshot = await getDocs(query(collection(db, "activities"), where("editorEmails", "array-contains", (currentUser.email || "").toLowerCase())));
    editorSnapshot.docs.forEach((item) => ids.add(item.id));
    const activities = (await Promise.all([...ids].map(async (id) => {
      const snapshot = await getDoc(doc(db, "activities", id));
      return snapshot.exists() ? { id: snapshot.id, ...snapshot.data() } : null;
    }))).filter(Boolean).sort((a, b) => toDate(a.startsAt) - toDate(b.startsAt));
    mineGrid.innerHTML = activities.map(activityCard).join("");
    mineGrid.querySelectorAll(".activity-card").forEach((card) => card.addEventListener("click", () => navigate(`activity/${card.dataset.id}`)));
    mineEmpty.hidden = activities.length > 0;
  } catch (error) {
    mineEmpty.hidden = false;
    showMessage(friendlyError(error), "error");
  }
}

function shareText() {
  const activity = currentActivity;
  const registered = currentRegistrations.filter((item) => item.status === "registered").length;
  const startsAt = toDate(activity.startsAt);
  const endsAt = activity.endsAt ? toDate(activity.endsAt) : null;
  const url = `${location.origin}/#activity/${activity.id}`;
  return `${activityEmoji(activity)} ${activity.title}\n📅 ${formatDate(startsAt)}${endsAt ? `～${formatTime(endsAt)}` : ""}\n📍 ${activity.location}\n💰 ${activity.fee ? `NT$ ${activity.fee}` : "免費"}\n👥 正取 ${registered}/${activity.maxPeople} 人${activity.visibility === "invite" ? `\n🔐 邀請碼：${activity.inviteCode}` : ""}\n👉 活動連結：${url}`;
}

async function copyShareText() {
  try {
    await navigator.clipboard.writeText(shareText());
    showMessage("活動資訊與連結已複製。", "success");
  } catch {
    showMessage("瀏覽器無法自動複製，請改用 LINE 分享。", "error");
  }
}

function shareToLine() {
  const url = `https://social-plugins.line.me/lineit/share?url=${encodeURIComponent(`${location.origin}/#activity/${currentActivity.id}`)}&text=${encodeURIComponent(shareText())}`;
  window.open(url, "_blank", "noopener,noreferrer");
}

function randomInviteCode() {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const values = crypto.getRandomValues(new Uint8Array(8));
  return [...values].map((value) => alphabet[value % alphabet.length]).join("");
}

function activityEmoji(activity) {
  const value = `${activity.activityType || ""} ${activity.title || ""}`.toLowerCase();
  const mappings = [
    [["羽球", "badminton"], "🏸"], [["籃球", "basketball"], "🏀"],
    [["棒球", "baseball"], "⚾"], [["足球", "soccer", "football"], "⚽"],
    [["排球", "volleyball"], "🏐"], [["網球", "tennis"], "🎾"],
    [["桌球", "乒乓", "ping pong"], "🏓"], [["保齡球", "bowling"], "🎳"],
    [["跑步", "路跑", "running"], "🏃"], [["單車", "自行車", "cycling"], "🚴"],
    [["爬山", "健行", "hiking"], "🥾"], [["游泳", "swimming"], "🏊"],
    [["桌遊", "board game"], "🎲"], [["電影", "movie"], "🎬"],
    [["聚餐", "吃飯", "美食", "dinner"], "🍽️"], [["唱歌", "ktv"], "🎤"]
  ];
  return mappings.find(([keywords]) => keywords.some((keyword) => value.includes(keyword)))?.[1] || "✨";
}

function toDate(value) {
  if (!value) return new Date(0);
  if (typeof value.toDate === "function") return value.toDate();
  return new Date(value);
}

function formatDate(date) {
  return new Intl.DateTimeFormat("zh-TW", { year: "numeric", month: "numeric", day: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

function formatTime(date) {
  return new Intl.DateTimeFormat("zh-TW", { hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

function localDateKey(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
}

if (!location.hash) location.hash = "home";
if (!configured) showView("home");
