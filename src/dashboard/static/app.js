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
