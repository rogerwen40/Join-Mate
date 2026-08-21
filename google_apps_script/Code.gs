const JOINMATE_REMINDER_URL = 'https://joinmate.onrender.com/api/reminders/run';
const FIREBASE_PROJECT_ID = 'joinmate-fire';
const FIREBASE_API_KEY = 'AIzaSyDr-VO4h77b10QdP94V0TQg5O5LqHqG0_g';
const FIREBASE_TIME_ZONE = 'Asia/Taipei';

function jsonResponse(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

function setupJoinMate() {
  const properties = PropertiesService.getScriptProperties();
  let secret = properties.getProperty('JOINMATE_EMAIL_SECRET');
  if (!secret) {
    secret = Utilities.getUuid() + Utilities.getUuid();
    properties.setProperty('JOINMATE_EMAIL_SECRET', secret);
  }
  ScriptApp.getProjectTriggers()
    .filter((trigger) => trigger.getHandlerFunction() === 'wakeJoinMate')
    .forEach((trigger) => ScriptApp.deleteTrigger(trigger));
  ScriptApp.newTrigger('wakeJoinMate').timeBased().everyMinutes(30).create();
  console.log('請複製到 Render 的 JOINMATE_EMAIL_SECRET：' + secret);
  return secret;
}

function setupJoinMateFirebase() {
  ScriptApp.getProjectTriggers()
    .filter((trigger) => trigger.getHandlerFunction() === 'wakeJoinMate')
    .forEach((trigger) => ScriptApp.deleteTrigger(trigger));
  console.log('JoinMate Firebase Mailer 設定完成。');
}

function doGet() {
  return jsonResponse({ok: true, service: 'JoinMate Mailer'});
}

function doPost(e) {
  try {
    const payload = JSON.parse((e.postData && e.postData.contents) || '{}');
    if (String(payload.action || '').indexOf('firebase_') === 0) {
      return handleFirebaseMail(payload);
    }
    return handleLegacyMail(payload);
  } catch (error) {
    console.error(error);
    return jsonResponse({ok: false, error: String(error.message || error)});
  }
}

function handleLegacyMail(payload) {
  const expectedSecret = PropertiesService.getScriptProperties()
    .getProperty('JOINMATE_EMAIL_SECRET');
  if (!expectedSecret || payload.secret !== expectedSecret) {
    return jsonResponse({ok: false, error: 'Unauthorized'});
  }
  if (payload.action !== 'send_email') {
    return jsonResponse({ok: false, error: 'Unsupported action'});
  }
  if (!payload.to || !payload.subject || !payload.body) {
    return jsonResponse({ok: false, error: 'Missing email fields'});
  }
  sendJoinMateEmail(payload.to, payload.subject, payload.body, payload.html_body || '');
  return jsonResponse({ok: true});
}

function handleFirebaseMail(payload) {
  const idToken = String(payload.idToken || '');
  const activityId = validateDocumentId(payload.activityId);
  if (!idToken || !activityId) {
    return jsonResponse({ok: false, error: 'Missing Firebase identity'});
  }
  const user = verifyFirebaseUser(idToken);
  const activity = getFirestoreDocument('activities/' + activityId, idToken);
  if (!activity) return jsonResponse({ok: false, error: 'Activity not found'});
  if (payload.action === 'firebase_registration') {
    return sendFirebaseRegistrationEmail(user, activityId, activity, idToken);
  }
  if (payload.action === 'firebase_registration_cancelled') {
    return sendFirebaseRegistrationCancelledEmail(user, activityId, activity);
  }
  if (payload.action === 'firebase_activity_changed') {
    return sendFirebaseActivityChangedEmails(user, activityId, activity, idToken);
  }
  return jsonResponse({ok: false, error: 'Unsupported Firebase action'});
}

function verifyFirebaseUser(idToken) {
  const response = UrlFetchApp.fetch(
    'https://identitytoolkit.googleapis.com/v1/accounts:lookup?key=' +
      encodeURIComponent(FIREBASE_API_KEY),
    {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({idToken: idToken}),
      muteHttpExceptions: true,
    }
  );
  if (response.getResponseCode() !== 200) throw new Error('Invalid Firebase login');
  const result = JSON.parse(response.getContentText());
  if (!result.users || !result.users.length || !result.users[0].email) {
    throw new Error('Firebase account has no email');
  }
  return result.users[0];
}

function sendFirebaseRegistrationEmail(user, activityId, activity, idToken) {
  const registration = getFirestoreDocument(
    'activities/' + activityId + '/registrations/' + validateDocumentId(user.localId),
    idToken
  );
  if (!registration || registration.userId !== user.localId) {
    return jsonResponse({ok: false, error: 'Registration not found'});
  }
  const cacheKey = 'jm-reg-' + user.localId + '-' + activityId;
  const cache = CacheService.getScriptCache();
  if (cache.get(cacheKey)) return jsonResponse({ok: true, skipped: 'duplicate'});

  const statusText = registration.status === 'waitlisted' ? '候補' : '正取';
  const subject = '[JoinMate] ' + (statusText === '正取' ? '報名成功' : '已加入候補') +
    '｜' + String(activity.title || '活動');
  const details = activityDetails(activity, activityId);
  const body = (user.displayName || user.email) + ' 您好：\n\n' +
    '你已完成「' + activity.title + '」的報名，目前狀態：' + statusText + '。\n\n' +
    details.text;
  const htmlBody = '<p>' + escapeHtml(user.displayName || user.email) + ' 您好：</p>' +
    '<p>你已完成「<strong>' + escapeHtml(activity.title) +
    '</strong>」的報名，目前狀態：<strong>' + statusText + '</strong>。</p>' +
    details.html;
  sendJoinMateEmail(user.email, subject, body, htmlBody);
  cache.put(cacheKey, '1', 21600);
  return jsonResponse({ok: true, sent: 1});
}

function sendFirebaseRegistrationCancelledEmail(user, activityId, activity) {
  const cacheKey = 'jm-reg-cancel-' + user.localId + '-' + activityId;
  const cache = CacheService.getScriptCache();
  if (cache.get(cacheKey)) return jsonResponse({ok: true, skipped: 'duplicate'});

  const subject = '[JoinMate] 已取消報名｜' + String(activity.title || '活動');
  const details = activityDetails(activity, activityId);
  const body = (user.displayName || user.email) + ' 您好：\n\n' +
    '你已取消「' + activity.title + '」的報名。\n\n' + details.text;
  const htmlBody = '<p>' + escapeHtml(user.displayName || user.email) + ' 您好：</p>' +
    '<p>你已取消「<strong>' + escapeHtml(activity.title) + '</strong>」的報名。</p>' +
    details.html;
  sendJoinMateEmail(user.email, subject, body, htmlBody);
  cache.put(cacheKey, '1', 300);
  return jsonResponse({ok: true, sent: 1});
}

function sendFirebaseActivityChangedEmails(user, activityId, activity, idToken) {
  const editorEmails = Array.isArray(activity.editorEmails) ? activity.editorEmails : [];
  const userEmail = String(user.email || '').toLowerCase();
  const mayEdit = activity.creatorId === user.localId ||
    editorEmails.map((email) => String(email).toLowerCase()).indexOf(userEmail) !== -1;
  if (!mayEdit) {
    return jsonResponse({ok: false, error: 'Only activity editors can notify members'});
  }
  const cacheKey = 'jm-edit-' + activityId;
  const cache = CacheService.getScriptCache();
  if (cache.get(cacheKey)) return jsonResponse({ok: true, skipped: 'duplicate'});

  const registrations = listFirestoreDocuments(
    'activities/' + activityId + '/registrations', idToken
  );
  const recipients = {};
  registrations.forEach((registration) => {
    if (registration.email) recipients[String(registration.email).toLowerCase()] = registration;
  });
  const cancelled = activity.status === 'cancelled';
  const eventText = cancelled ? '活動已取消' : '活動內容已更新';
  const subject = '[JoinMate] ' + eventText + '｜' + String(activity.title || '活動');
  const details = activityDetails(activity, activityId);
  let sent = 0;
  Object.keys(recipients).forEach((email) => {
    const registration = recipients[email];
    const body = (registration.displayName || email) + ' 您好：\n\n' +
      '你參加的「' + activity.title + '」' + eventText + '，請確認最新資訊。\n\n' +
      details.text;
    const htmlBody = '<p>' + escapeHtml(registration.displayName || email) + ' 您好：</p>' +
      '<p>你參加的「<strong>' + escapeHtml(activity.title) + '</strong>」' +
      eventText + '，請確認最新資訊。</p>' + details.html;
    sendJoinMateEmail(email, subject, body, htmlBody);
    sent += 1;
  });
  cache.put(cacheKey, '1', 30);
  return jsonResponse({ok: true, sent: sent});
}

function getFirestoreDocument(path, idToken) {
  const url = firestoreBaseUrl() + '/' + path.split('/').map(encodeURIComponent).join('/');
  const response = firebaseAuthorizedFetch(url, idToken);
  if (response.getResponseCode() === 404) return null;
  if (response.getResponseCode() !== 200) {
    throw new Error('Firestore read failed: ' + response.getResponseCode());
  }
  return decodeFirestoreDocument(JSON.parse(response.getContentText()));
}

function listFirestoreDocuments(path, idToken) {
  const baseUrl = firestoreBaseUrl() + '/' + path.split('/').map(encodeURIComponent).join('/');
  let pageToken = '';
  let documents = [];
  do {
    const url = baseUrl + '?pageSize=100' +
      (pageToken ? '&pageToken=' + encodeURIComponent(pageToken) : '');
    const response = firebaseAuthorizedFetch(url, idToken);
    if (response.getResponseCode() !== 200) {
      throw new Error('Firestore list failed: ' + response.getResponseCode());
    }
    const result = JSON.parse(response.getContentText());
    documents = documents.concat((result.documents || []).map(decodeFirestoreDocument));
    pageToken = result.nextPageToken || '';
  } while (pageToken);
  return documents;
}

function firebaseAuthorizedFetch(url, idToken) {
  return UrlFetchApp.fetch(url, {
    method: 'get',
    headers: {Authorization: 'Bearer ' + idToken},
    muteHttpExceptions: true,
  });
}

function firestoreBaseUrl() {
  return 'https://firestore.googleapis.com/v1/projects/' + FIREBASE_PROJECT_ID +
    '/databases/(default)/documents';
}

function decodeFirestoreDocument(document) {
  return decodeFirestoreFields(document.fields || {});
}

function decodeFirestoreFields(fields) {
  const result = {};
  Object.keys(fields).forEach((key) => {
    result[key] = decodeFirestoreValue(fields[key]);
  });
  return result;
}

function decodeFirestoreValue(value) {
  if (Object.prototype.hasOwnProperty.call(value, 'stringValue')) return value.stringValue;
  if (Object.prototype.hasOwnProperty.call(value, 'integerValue')) return Number(value.integerValue);
  if (Object.prototype.hasOwnProperty.call(value, 'doubleValue')) return Number(value.doubleValue);
  if (Object.prototype.hasOwnProperty.call(value, 'booleanValue')) return value.booleanValue;
  if (Object.prototype.hasOwnProperty.call(value, 'timestampValue')) return value.timestampValue;
  if (Object.prototype.hasOwnProperty.call(value, 'nullValue')) return null;
  if (value.arrayValue) return (value.arrayValue.values || []).map(decodeFirestoreValue);
  if (value.mapValue) return decodeFirestoreFields(value.mapValue.fields || {});
  return null;
}

function activityDetails(activity, activityId) {
  const start = activity.startsAt ?
    Utilities.formatDate(new Date(activity.startsAt), FIREBASE_TIME_ZONE, 'yyyy/MM/dd HH:mm') : '未設定';
  const end = activity.endsAt ?
    Utilities.formatDate(new Date(activity.endsAt), FIREBASE_TIME_ZONE, 'HH:mm') : '';
  const fee = Number(activity.fee || 0) > 0 ? 'NT$ ' + Number(activity.fee) : '免費';
  const url = 'https://joinmate-fire.web.app/#activity/' + encodeURIComponent(activityId);
  return {
    text: '活動：' + activity.title + '\n' +
      '時間：' + start + (end ? '～' + end : '') + '\n' +
      '地點：' + activity.location + '\n' +
      '費用：' + fee + '\n' +
      '活動連結：' + url,
    html: '<div style="line-height:1.8">' +
      '<strong>活動：</strong>' + escapeHtml(activity.title) + '<br>' +
      '<strong>時間：</strong>' + escapeHtml(start + (end ? '～' + end : '')) + '<br>' +
      '<strong>地點：</strong>' + escapeHtml(activity.location) + '<br>' +
      '<strong>費用：</strong>' + escapeHtml(fee) + '<br>' +
      '<a href="' + url + '">查看活動內容</a></div>',
  };
}

function sendJoinMateEmail(to, subject, body, htmlBody) {
  MailApp.sendEmail({
    to: String(to),
    subject: String(subject).slice(0, 200),
    body: String(body),
    htmlBody: String(htmlBody || ''),
    name: 'JoinMate',
    replyTo: Session.getEffectiveUser().getEmail(),
  });
}

function validateDocumentId(value) {
  const id = String(value || '');
  return /^[A-Za-z0-9_-]{1,160}$/.test(id) ? id : '';
}

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function wakeJoinMate() {
  const secret = PropertiesService.getScriptProperties()
    .getProperty('JOINMATE_EMAIL_SECRET');
  if (!secret) throw new Error('請先執行 setupJoinMate');
  const response = UrlFetchApp.fetch(JOINMATE_REMINDER_URL, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({secret: secret}),
    muteHttpExceptions: true,
  });
  console.log(response.getResponseCode() + ' ' + response.getContentText());
}
