const JOINMATE_REMINDER_URL =
  'https://joinmate.onrender.com/api/reminders/run';

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
  ScriptApp.newTrigger('wakeJoinMate').timeBased().everyMinutes(5).create();

  console.log('請複製到 Render 的 JOINMATE_EMAIL_SECRET：' + secret);
  return secret;
}

function doGet() {
  return jsonResponse({ok: true, service: 'JoinMate Mailer'});
}

function doPost(e) {
  try {
    const payload = JSON.parse(e.postData.contents || '{}');
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

    MailApp.sendEmail({
      to: String(payload.to),
      subject: String(payload.subject).slice(0, 200),
      body: String(payload.body),
      htmlBody: String(payload.html_body || ''),
      name: 'JoinMate',
      replyTo: Session.getEffectiveUser().getEmail(),
    });
    return jsonResponse({ok: true});
  } catch (error) {
    console.error(error);
    return jsonResponse({ok: false, error: String(error.message || error)});
  }
}

function wakeJoinMate() {
  const secret = PropertiesService.getScriptProperties()
    .getProperty('JOINMATE_EMAIL_SECRET');
  if (!secret) {
    throw new Error('請先執行 setupJoinMate');
  }

  const response = UrlFetchApp.fetch(JOINMATE_REMINDER_URL, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({secret: secret}),
    muteHttpExceptions: true,
  });
  console.log(response.getResponseCode() + ' ' + response.getContentText());
}
