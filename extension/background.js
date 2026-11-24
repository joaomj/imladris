// Send the full reading list to the Imladris native host whenever it changes,
// when Brave starts, and once a day, so the export's age shows Brave is running.
const HOST = "com.imladris.reading_list";
const DAILY = "daily-export";

let pending = Promise.resolve();

async function exportList() {
  const entries = await chrome.readingList.query({});
  const reply = await chrome.runtime.sendNativeMessage(HOST, { entries });
  if (!reply?.ok) {
    throw new Error(`Imladris host rejected the export: ${reply?.error ?? "no reply"}`);
  }
}

// Serialize exports so an older list never replaces a newer one.
function exportSoon() {
  pending = pending.then(exportList).catch((error) => console.error(error));
}

// Alarms may not survive a browser restart, so recreate a missing one.
async function ensureDailyAlarm() {
  if (!(await chrome.alarms.get(DAILY))) {
    await chrome.alarms.create(DAILY, { periodInMinutes: 24 * 60 });
  }
}

for (const event of [chrome.runtime.onInstalled, chrome.runtime.onStartup]) {
  event.addListener(() => {
    ensureDailyAlarm();
    exportSoon();
  });
}
chrome.alarms.onAlarm.addListener(exportSoon);
chrome.readingList.onEntryAdded.addListener(exportSoon);
chrome.readingList.onEntryUpdated.addListener(exportSoon);
chrome.readingList.onEntryRemoved.addListener(exportSoon);
