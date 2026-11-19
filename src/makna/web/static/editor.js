// The editor page's one script: it puts the Ace editor in place of the text area. The page works
// without it (the text area is the editor, and Check, Save draft, and Submit are form posts), so
// if Ace did not load, nothing here runs and nothing is lost. Ace's styles are two files the page
// links (`ace.css`, `textmate.css`), so the content security policy needs no change; its worker
// (a blob) is not used.
(function () {
  "use strict";
  var area = document.getElementById("yaml_text");
  var form = document.getElementById("editor-form");
  if (!area || !form || typeof ace === "undefined") {
    return;
  }
  ace.config.set("useStrictCSP", true);
  ace.config.set("basePath", "/static/vendor/ace");
  var holder = document.createElement("div");
  holder.className = "editor-box";
  area.parentNode.insertBefore(holder, area.nextSibling);
  var editor = ace.edit(holder, {
    mode: "ace/mode/yaml",
    theme: "ace/theme/textmate",
    useWorker: false,
    showPrintMargin: false,
    showFoldWidgets: false,
    tabSize: 2,
    useSoftTabs: true,
    wrap: false
  });
  editor.setValue(area.value, -1);
  editor.textInput.getElement().setAttribute("aria-label", "Definition (YAML)");
  area.hidden = true;
  // Before any button sends the form, the text area gets Ace's text.
  form.addEventListener("submit", function () {
    area.value = editor.getValue();
  });
})();
