// The only client-side script in the app, and it does one thing: start a job
// without a page reload and then poll until it finishes. A backfill runs for
// minutes, so a plain form post would hold the page hostage to it.
//
// Deliberately dependency-free. Vendoring htmx to avoid twenty lines of fetch
// would put a downloaded bundle in the repo for no gain.

(function () {
  var POLL_MS = 2000;
  var polling = false;

  function setPill(row, status) {
    var cell = row.querySelector('.status-cell');
    if (cell) cell.innerHTML = '<span class="pill ' + status + '">' + status + '</span>';
  }

  document.querySelectorAll('.run-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var job = btn.dataset.job;
      var row = document.querySelector('tr[data-job="' + job + '"]');
      btn.disabled = true;
      btn.textContent = 'Starting…';
      fetch('/jobs/' + encodeURIComponent(job) + '/run', { method: 'POST' })
        .then(function (r) { return r.json().then(function (b) { return [r.ok, b]; }); })
        .then(function (pair) {
          if (!pair[0]) {
            btn.textContent = pair[1].reason || 'Failed';
            setTimeout(reset.bind(null, btn), 2500);
            return;
          }
          btn.textContent = 'Running…';
          if (row) setPill(row, 'running');
          startPolling();
        })
        .catch(function () {
          btn.textContent = 'Failed';
          setTimeout(reset.bind(null, btn), 2500);
        });
    });
  });

  function reset(btn) {
    btn.disabled = false;
    btn.textContent = 'Run now';
  }

  // Refresh preview: same pattern as a job, but per-article and started from
  // the article page. Submitting normally would hold the page open for the
  // minute or two the model takes.
  var previewForm = document.querySelector('.preview-form');
  if (previewForm) {
    previewForm.addEventListener('submit', function (event) {
      event.preventDefault();
      var id = previewForm.dataset.article;
      var btn = previewForm.querySelector('.preview-btn');
      btn.disabled = true;
      btn.textContent = 'Running…';
      fetch('/blog/' + encodeURIComponent(id) + '/preview', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (body) {
          if (!body.started) {
            btn.textContent = body.reason || 'Already running';
            return;
          }
          pollPreview(id);
        })
        .catch(function () { btn.textContent = 'Failed'; });
    });
  }

  function pollPreview(id) {
    var tick = function () {
      fetch('/blog/' + encodeURIComponent(id) + '/preview/status')
        .then(function (r) { return r.json(); })
        .then(function (state) {
          if (state.running) {
            setTimeout(tick, 3000);
          } else {
            // Reload so the diff, token counts and status all come from the
            // server rather than being assembled here.
            window.location.reload();
          }
        })
        .catch(function () { setTimeout(tick, 6000); });
    };
    setTimeout(tick, 3000);
  }

  // Product import: one pass of work per request, driven from here.
  //
  // The run page is what makes an import feel like it's happening — each
  // pass creates a few more products and the page says so. It is also what
  // *does* the work in practice: the server never starts a background thread
  // for it (a Vercel function is frozen the moment it responds), so an
  // import advances when this loop asks it to, or overnight when the cron
  // job does. Closing the tab pauses it; it never loses anything.
  var importRun = document.querySelector('.import-run');
  if (importRun) {
    var runId = importRun.dataset.run;
    var active = importRun.dataset.active === '1';
    var advanceBtn = document.querySelector('.js-advance');
    var advancing = false;

    var setText = function (selector, value) {
      var node = document.querySelector(selector);
      if (node && value !== undefined && value !== null) node.textContent = value;
    };

    var renderLog = function (lines) {
      var list = document.querySelector('.js-log');
      if (!list || !lines) return;
      list.innerHTML = '';
      lines.forEach(function (line) {
        var li = document.createElement('li');
        li.textContent = line;
        list.appendChild(li);
      });
    };

    var advance = function () {
      if (advancing) return;
      advancing = true;
      if (advanceBtn) { advanceBtn.disabled = true; advanceBtn.textContent = 'Working…'; }
      fetch('/import/' + encodeURIComponent(runId) + '/advance', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (body) {
          advancing = false;
          if (advanceBtn) { advanceBtn.disabled = false; advanceBtn.textContent = 'Continue now'; }
          // A failed run used to end the loop here and say nothing, which
          // left the page frozen on whatever stage it was last rendered
          // with — indistinguishable from an import still working. Reload
          // instead: the server already renders the failure banner, the
          // final stage and the log, so there's no second copy of that UI
          // to keep in step. `active` is false on a failed run, so the
          // reloaded page doesn't start the loop again.
          if (body.error) { window.location.reload(); return; }
          setText('.stage-value', body.stage);
          setText('.js-stage-note', body.message || (body.active ? 'working…' : 'finished'));
          if (body.counts) {
            setText('.js-count-created', body.dry_run ? body.counts.prepared : body.counts.created);
            setText('.js-count-skipped', body.counts.skipped);
            // Only present once something has been rewritten — the card is
            // rendered on the server and setText ignores a missing node, so
            // the first rewrite shows up on the reload at the end of the run.
            setText('.js-count-updated', body.counts.updated);
            setText('.js-count-failed', body.counts.failed);
          }
          renderLog(body.log);
          if (body.active) {
            // A short breath between passes rather than none: each pass is
            // already tens of seconds of fetching, and hammering a
            // supplier's site from a UI loop is the thing the scraper's
            // pacing exists to prevent.
            setTimeout(advance, 1500);
          } else {
            // Finished — reload so the product table, its thumbnails and
            // every link come from the server rather than being patched in.
            window.location.reload();
          }
        })
        .catch(function () {
          advancing = false;
          if (advanceBtn) { advanceBtn.disabled = false; advanceBtn.textContent = 'Continue now'; }
          setTimeout(advance, 5000);
        });
    };

    if (advanceBtn) advanceBtn.addEventListener('click', advance);
    if (active) setTimeout(advance, 400);
  }

  function startPolling() {
    if (polling) return;
    polling = true;
    var tick = function () {
      fetch('/jobs/status')
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var anyRunning = false;
          Object.keys(data.jobs).forEach(function (name) {
            var state = data.jobs[name];
            var row = document.querySelector('tr[data-job="' + name + '"]');
            if (!row) return;
            if (state.running) {
              anyRunning = true;
              setPill(row, 'running');
            }
          });
          if (anyRunning) {
            setTimeout(tick, POLL_MS);
          } else {
            // Finished. Reload so the run log, row counts and detail blob all
            // come from the server rather than being half-patched in here.
            window.location.reload();
          }
        })
        .catch(function () { setTimeout(tick, POLL_MS * 2); });
    };
    setTimeout(tick, POLL_MS);
  }
})();
