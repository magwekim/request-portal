/* Vanilla JavaScript: every feature here is wired to a real control. */
(function () {
  "use strict";

  // 1. Mobile navigation toggle.
  var toggle = document.querySelector(".nav-toggle");
  var nav = document.getElementById("site-nav");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // 2. Confirmation before sensitive actions.
  //    <form data-confirm="..."> always asks. With data-confirm-select + data-confirm-values
  //    it only asks when the named <select> has one of the listed values.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var selectName = form.getAttribute("data-confirm-select");
      if (selectName) {
        var select = form.elements[selectName];
        var values = (form.getAttribute("data-confirm-values") || "").split(",");
        if (!select || values.indexOf(select.value) === -1) { return; }
      }
      if (!window.confirm(form.getAttribute("data-confirm"))) { event.preventDefault(); }
    });
  });

  // 3. Dependent selects: <select data-dept-source="targetId[,targetId]"> filters the
  //    <option data-dept="..."> items of the target selects (server re-validates everything).
  document.querySelectorAll("select[data-dept-source]").forEach(function (source) {
    var targets = source.getAttribute("data-dept-source").split(",").map(function (id) {
      var el = document.getElementById(id.trim());
      return el ? { el: el, options: Array.prototype.slice.call(el.options) } : null;
    }).filter(Boolean);

    function apply() {
      targets.forEach(function (t) {
        var previous = t.el.value;
        t.el.innerHTML = "";
        t.options.forEach(function (opt) {
          var dept = opt.getAttribute("data-dept");
          if (dept === null || source.value === "" || dept === source.value) { t.el.appendChild(opt); }
        });
        var stillThere = Array.prototype.some.call(t.el.options, function (o) { return o.value === previous; });
        t.el.value = stillThere ? previous : (t.el.options.length && t.el.options[0].value === "" ? "" : (t.el.options[0] || {}).value);
      });
    }
    source.addEventListener("change", apply);
    apply();
  });

  // 4. Show the "resolution details" box only when "Resolved" is chosen.
  var statusSelect = document.querySelector("select[data-toggle-resolution]");
  var resolutionField = document.getElementById("resolution-field");
  if (statusSelect && resolutionField) {
    var sync = function () { resolutionField.hidden = statusSelect.value !== "Resolved"; };
    statusSelect.addEventListener("change", sync);
    sync();
  }

  // 5. File pickers: preview what was chosen, shrink big photos so they fit the upload limit, and
  //    keep drag and drop working. The server still checks every file (type, signature, size).
  var PHOTO = /^image\/(png|jpeg)$/;

  function loadBitmap(file) {
    if (window.createImageBitmap) { return window.createImageBitmap(file); }
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file), img = new Image();
      img.onload = function () { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error("unreadable")); };
      img.src = url;
    });
  }

  function drawJpeg(bitmap, scale, quality) {
    var w = Math.max(1, Math.round(bitmap.width * scale)), h = Math.max(1, Math.round(bitmap.height * scale));
    var canvas = document.createElement("canvas");
    canvas.width = w; canvas.height = h;
    var ctx = canvas.getContext("2d");
    ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, w, h);          // PNG transparency becomes white
    ctx.drawImage(bitmap, 0, 0, w, h);
    return new Promise(function (resolve) { canvas.toBlob(resolve, "image/jpeg", quality); });
  }

  // Re-encode a photo as JPEG, lowering quality and then size until it fits under maxBytes.
  function shrinkPhoto(file, maxBytes) {
    return loadBitmap(file).then(function (bitmap) {
      var scale = Math.min(1, 2000 / Math.max(bitmap.width, bitmap.height)), quality = 0.86, tries = 0;
      function attempt() {
        return drawJpeg(bitmap, scale, quality).then(function (blob) {
          tries += 1;
          if (blob && blob.size <= maxBytes * 0.97) {
            return new File([blob], file.name.replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" });
          }
          if (tries >= 10) { return null; }
          if (quality > 0.62) { quality -= 0.08; } else { scale *= 0.8; }
          return attempt();
        });
      }
      return attempt();
    });
  }

  function thumbnail(file) {
    return new Promise(function (resolve) {
      if (!PHOTO.test(file.type)) { resolve(null); return; }
      var reader = new FileReader();
      reader.onload = function () { resolve(reader.result); };   // a data: URL, allowed by the site's policy
      reader.onerror = function () { resolve(null); };
      reader.readAsDataURL(file);
    });
  }

  function formatSize(bytes) {
    return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(bytes / 1024)) + " KB";
  }

  document.querySelectorAll("input[type=file][data-max-bytes]").forEach(function (input) {
    var max = parseInt(input.getAttribute("data-max-bytes"), 10);
    var maxFiles = parseInt(input.getAttribute("data-max-files") || "3", 10);
    var field = input.closest(".field");
    var zone = input.closest("[data-dropzone]");
    var list = field && field.querySelector("[data-previews]");
    var note = field && field.querySelector("[data-file-note]");
    var form = input.form;
    var busy = false;

    function say(text) { if (note) { note.textContent = text || ""; } }

    function setFiles(files) {
      var transfer = new DataTransfer();
      files.forEach(function (f) { transfer.items.add(f); });
      input.files = transfer.files;
    }

    function render() {
      if (!list) { return; }
      list.innerHTML = "";
      Array.prototype.forEach.call(input.files, function (file, index) {
        var li = document.createElement("li");
        li.className = "file-preview";
        var holder = document.createElement("span");
        holder.className = PHOTO.test(file.type) ? "file-preview-thumb" : "file-preview-doc";
        if (!PHOTO.test(file.type)) { holder.textContent = "PDF"; }
        li.appendChild(holder);
        var text = document.createElement("span");
        text.className = "file-preview-text";
        var name = document.createElement("span");
        name.className = "file-preview-name"; name.textContent = file.name;
        var size = document.createElement("span");
        size.className = "muted"; size.textContent = formatSize(file.size);
        text.appendChild(name); text.appendChild(size);
        li.appendChild(text);
        var remove = document.createElement("button");
        remove.type = "button"; remove.className = "btn btn-secondary btn-small";
        remove.textContent = "Remove";
        remove.setAttribute("aria-label", "Remove " + file.name);
        remove.addEventListener("click", function () {
          var rest = Array.prototype.slice.call(input.files);
          rest.splice(index, 1);
          setFiles(rest); say(""); render();
        });
        li.appendChild(remove);
        list.appendChild(li);
        if (PHOTO.test(file.type)) {
          thumbnail(file).then(function (url) {
            if (!url) { return; }
            var img = document.createElement("img");
            img.src = url; img.alt = "";
            holder.appendChild(img);
          });
        }
      });
    }

    function handle(chosen) {
      var files = Array.prototype.slice.call(chosen);
      var problems = [], shrunk = 0;
      if (files.length > maxFiles) {
        problems.push("Only the first " + maxFiles + " files were kept.");
        files = files.slice(0, maxFiles);
      }
      busy = true; say("Preparing your files\u2026");
      Promise.all(files.map(function (file) {
        if (file.size <= max) { return Promise.resolve(file); }
        if (!PHOTO.test(file.type)) {
          problems.push("'" + file.name + "' is larger than " + formatSize(max) + " and was not added.");
          return Promise.resolve(null);
        }
        return shrinkPhoto(file, max).then(function (small) {
          if (small) { shrunk += 1; return small; }
          problems.push("'" + file.name + "' could not be made small enough. Try a smaller photo.");
          return null;
        }).catch(function () {
          problems.push("'" + file.name + "' could not be read as a photo.");
          return null;
        });
      })).then(function (ready) {
        setFiles(ready.filter(Boolean));
        var messages = problems.slice();
        if (shrunk) { messages.unshift(shrunk === 1 ? "A large photo was shrunk to fit." : shrunk + " large photos were shrunk to fit."); }
        say(messages.join(" "));
        render();
      }).then(function () { busy = false; }, function () { busy = false; });
    }

    input.addEventListener("change", function () { handle(input.files); });

    if (zone) {
      ["dragenter", "dragover"].forEach(function (type) {
        zone.addEventListener(type, function (event) { event.preventDefault(); zone.classList.add("is-over"); });
      });
      ["dragleave", "drop"].forEach(function (type) {
        zone.addEventListener(type, function () { zone.classList.remove("is-over"); });
      });
      // Dropping straight onto the (invisible) input already works; this covers the rest of the box.
      zone.addEventListener("drop", function (event) {
        if (event.target === input) { return; }
        event.preventDefault();
        if (event.dataTransfer && event.dataTransfer.files.length) { handle(event.dataTransfer.files); }
      });
    }

    if (form) {
      form.addEventListener("submit", function (event) {
        if (busy) { event.preventDefault(); say("Please wait a moment while your photos are prepared."); }
      });
    }
  });

  // 6. Photo viewer for attachment thumbnails: <a data-lightbox href="photo" data-caption="name">.
  var links = Array.prototype.slice.call(document.querySelectorAll("a[data-lightbox]"));
  if (links.length && typeof HTMLDialogElement === "function") {
    var dialog = document.createElement("dialog");
    dialog.className = "viewer";
    dialog.setAttribute("aria-label", "Photo viewer");
    dialog.innerHTML =
      '<figure class="viewer-figure"><img class="viewer-img" alt=""><figcaption class="viewer-caption"></figcaption></figure>' +
      '<button type="button" class="viewer-btn viewer-close" aria-label="Close viewer">&times;</button>' +
      '<button type="button" class="viewer-btn viewer-prev" aria-label="Previous photo">&lsaquo;</button>' +
      '<button type="button" class="viewer-btn viewer-next" aria-label="Next photo">&rsaquo;</button>';
    document.body.appendChild(dialog);
    var viewerImg = dialog.querySelector(".viewer-img");
    var viewerCaption = dialog.querySelector(".viewer-caption");
    var current = 0;

    var show = function (index) {
      current = (index + links.length) % links.length;
      viewerImg.src = links[current].getAttribute("href");
      viewerImg.alt = links[current].getAttribute("data-caption") || "";
      viewerCaption.textContent = (links[current].getAttribute("data-caption") || "") +
        (links.length > 1 ? "  (" + (current + 1) + " of " + links.length + ")" : "");
      dialog.classList.toggle("single", links.length < 2);
    };
    links.forEach(function (link, index) {
      link.addEventListener("click", function (event) {
        event.preventDefault();
        show(index);
        dialog.showModal();
      });
    });
    dialog.querySelector(".viewer-close").addEventListener("click", function () { dialog.close(); });
    dialog.querySelector(".viewer-prev").addEventListener("click", function () { show(current - 1); });
    dialog.querySelector(".viewer-next").addEventListener("click", function () { show(current + 1); });
    dialog.addEventListener("click", function (event) { if (event.target === dialog) { dialog.close(); } });
    dialog.addEventListener("keydown", function (event) {
      if (event.key === "ArrowLeft") { show(current - 1); }
      if (event.key === "ArrowRight") { show(current + 1); }
    });
  }
})();
