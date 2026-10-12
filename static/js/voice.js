/* voice.js — push-to-talk voice command handler using Web Speech API fallback
   (Vosk is server-side; this module handles the browser PTT → transcript → confirm flow) */

const Voice = (() => {
  let recognition = null;
  let _onResult = null;   // callback(transcript)
  let _statusEl = null;

  function init(statusEl, onResult) {
    _statusEl = statusEl;
    _onResult = onResult;

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      if (_statusEl) _statusEl.textContent = 'Voice not supported in this browser.';
      return false;
    }
    recognition = new SpeechRecognition();
    recognition.lang = 'en-IN';
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;

    recognition.onresult = (e) => {
      const transcript = e.results[0][0].transcript.trim().toLowerCase();
      if (_statusEl) _statusEl.textContent = `Heard: "${transcript}"`;
      if (_onResult) _onResult(transcript);
    };

    recognition.onerror = () => {
      if (_statusEl) _statusEl.textContent = "Sorry, I didn't catch that — please try again or use the touch screen.";
      _setListening(false);
    };

    recognition.onend = () => _setListening(false);
    return true;
  }

  function _setListening(on) {
    const btn = document.getElementById('voice-btn');
    if (btn) btn.classList.toggle('listening', on);
  }

  function start() {
    if (!recognition) return;
    _setListening(true);
    if (_statusEl) _statusEl.textContent = 'Listening…';
    recognition.start();
  }

  function stop() {
    if (!recognition) return;
    recognition.stop();
    _setListening(false);
  }

  /* Parse a transcript into an intent object or null */
  function parse(transcript, menuItems) {
    // "order <qty> <item> for <slot>"
    const orderRe = /order\s+(\w+)\s+(.+?)\s+for\s+(\d{1,2}:\d{2})/i;
    let m = transcript.match(orderRe);
    if (m) {
      const qty  = _parseQty(m[1]);
      const item = _matchItem(m[2], menuItems);
      return item ? { intent: 'order', qty, item, slot: m[3] } : null;
    }

    if (/wait\s*time/i.test(transcript))
      return { intent: 'wait_time' };

    // "mark <item> sold out"
    const soldRe = /mark\s+(.+?)\s+sold\s*out/i;
    m = transcript.match(soldRe);
    if (m) {
      const item = _matchItem(m[1], menuItems);
      return item ? { intent: 'sold_out', item } : null;
    }

    // "log <qty> leftover <item>"
    const wasteRe = /log\s+(\w+)\s+leftover\s+(.+)/i;
    m = transcript.match(wasteRe);
    if (m) {
      const qty  = _parseQty(m[1]);
      const item = _matchItem(m[2], menuItems);
      return item ? { intent: 'log_waste', qty, item } : null;
    }

    if (/prep\s*plan/i.test(transcript))
      return { intent: 'show_prep_plan' };

    return null;
  }

  function _parseQty(word) {
    const map = { one:1, two:2, three:3, four:4, five:5, six:6, seven:7, eight:8, nine:9, ten:10 };
    return map[word.toLowerCase()] || parseInt(word) || 1;
  }

  function _matchItem(phrase, menuItems) {
    if (!menuItems || !menuItems.length) return phrase;
    phrase = phrase.toLowerCase().trim();
    // exact match first
    let found = menuItems.find(i => i.name.toLowerCase() === phrase);
    if (found) return found;
    // partial match
    found = menuItems.find(i => i.name.toLowerCase().includes(phrase) || phrase.includes(i.name.toLowerCase()));
    return found || null;
  }

  return { init, start, stop, parse };
})();
