// AAMS 前端便利功能（無任何安全決策；所有限制皆由後端驗證）
(function () {
  "use strict";

  function fmtMB(n) { return (n / 1000000).toFixed(2) + " MB"; }

  // 上傳：顯示目前總量與剩餘額度
  document.querySelectorAll("input[type=file][data-max-total]").forEach(function (input) {
    var max = parseInt(input.dataset.maxTotal, 10);
    var maxFiles = parseInt(input.dataset.maxFiles || "1", 10);
    var info = document.getElementById(input.dataset.info);
    var meter = document.getElementById(input.dataset.meter);
    var submit = input.form.querySelector("button[type=submit]");
    input.addEventListener("change", function () {
      var total = 0;
      var names = [];
      for (var i = 0; i < input.files.length; i++) {
        total += input.files[i].size;
        names.push(input.files[i].name + "（" + fmtMB(input.files[i].size) + "）");
      }
      var over = total > max;
      var tooMany = input.files.length > maxFiles;
      if (info) {
        info.textContent = "已選 " + input.files.length + " 個檔案，合計 " + fmtMB(total) + " / 上限 " + fmtMB(max) +
          (over ? " — 超過上限，整次提交將被拒絕" : "，剩餘 " + fmtMB(max - total)) +
          (tooMany ? "；檔案數超過上限 " + maxFiles : "");
      }
      if (meter) {
        meter.classList.toggle("over", over || tooMany);
        meter.firstElementChild.style.width = Math.min(100, (total / max) * 100) + "%";
      }
      var list = document.getElementById(input.dataset.list);
      if (list) {
        list.innerHTML = "";
        names.forEach(function (n) { var li = document.createElement("li"); li.textContent = n; list.appendChild(li); });
      }
      if (submit) submit.disabled = over || tooMany || input.files.length === 0;
    });
  });

  // Rubric 一鍵填分
  document.querySelectorAll("button[data-rubric-score]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      var form = btn.closest("form");
      form.querySelector("input[name=score]").value = btn.dataset.rubricScore;
      var fb = form.querySelector("textarea[name=feedback]");
      if (fb && btn.dataset.rubricFeedback) {
        fb.value = fb.value ? fb.value + "\n" + btn.dataset.rubricFeedback : btn.dataset.rubricFeedback;
      }
    });
  });

  // 危險操作確認
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  // 多檔核取框 → 啟用檔案數欄位
  var multi = document.getElementById("allow_multiple_files");
  var maxFiles = document.getElementById("max_files");
  if (multi && maxFiles) {
    var sync = function () { maxFiles.disabled = !multi.checked; if (!multi.checked) maxFiles.value = 1; };
    multi.addEventListener("change", sync);
    sync();
  }

  // 個資告知：捲到底才能勾選（無 JS 時不鎖定；後端仍要求逐項勾選）
  document.querySelectorAll("form[data-scroll-gate]").forEach(function (form) {
    var box = document.getElementById(form.dataset.scrollGate);
    var hint = document.getElementById("consent-hint");
    var boxes = form.querySelectorAll("input[type=checkbox]");
    if (!box) return;
    var unlock = function () {
      boxes.forEach(function (c) { c.disabled = false; });
      if (hint) hint.textContent = "已讀完，請逐項勾選。";
    };
    var check = function () {
      if (box.scrollTop + box.clientHeight >= box.scrollHeight - 8) { unlock(); box.removeEventListener("scroll", check); }
    };
    boxes.forEach(function (c) { c.disabled = true; });
    box.addEventListener("scroll", check);
    check();  // 內容很短、不需要捲動時直接解鎖
  });

  // 對話框（<dialog>）
  document.querySelectorAll("[data-open-dialog]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var d = document.getElementById(btn.dataset.openDialog);
      if (d && d.showModal) d.showModal(); else if (d) window.location.href = "/privacy";
    });
  });
  document.querySelectorAll("[data-close-dialog]").forEach(function (btn) {
    btn.addEventListener("click", function () { var d = btn.closest("dialog"); if (d) d.close(); });
  });

  // 倒數計時：以伺服器算好的剩餘秒數為準（不信任使用者電腦時鐘）
  var t0 = (window.performance && performance.now) ? performance.now() : Date.now();
  var elapsed = function () { return (((window.performance && performance.now) ? performance.now() : Date.now()) - t0) / 1000; };
  var pad = function (n) { return (n < 10 ? "0" : "") + n; };
  var countdowns = document.querySelectorAll("[data-countdown]");
  var clocks = document.querySelectorAll("[data-server-epoch]");
  var tick = function () {
    countdowns.forEach(function (el) {
      var left = Math.floor(parseFloat(el.dataset.countdown) - elapsed());
      if (left <= 0) {
        el.textContent = el.dataset.doneText || "已截止";
        el.classList.add("cd-done");
        el.classList.remove("cd-warn");
        return;
      }
      var d = Math.floor(left / 86400), h = Math.floor(left % 86400 / 3600), m = Math.floor(left % 3600 / 60), s = left % 60;
      el.textContent = "剩 " + (d ? d + " 天 " : "") + pad(h) + ":" + pad(m) + ":" + pad(s);
      el.classList.toggle("cd-warn", left <= 3600);
    });
    clocks.forEach(function (el) {
      var t = new Date(parseInt(el.dataset.serverEpoch, 10) + elapsed() * 1000);
      try {
        el.textContent = t.toLocaleString("zh-TW", { timeZone: "Asia/Taipei", hour12: false });
      } catch (e) { el.textContent = t.toISOString(); }
    });
  };
  if (countdowns.length || clocks.length) { tick(); setInterval(tick, 1000); }

  // 批次下載：全選／全不選
  document.querySelectorAll("[data-check-all]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      var on = btn.dataset.checkAll === "1";
      document.querySelectorAll("input[name=units]").forEach(function (c) { c.checked = on; });
    });
  });

  // 複製收據
  document.querySelectorAll("button[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var el = document.getElementById(btn.dataset.copy);
      if (!el || !navigator.clipboard) return;
      navigator.clipboard.writeText(el.innerText).then(function () { btn.textContent = "已複製"; });
    });
  });
})();
