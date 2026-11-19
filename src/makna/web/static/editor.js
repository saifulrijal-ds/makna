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
  // Ace loads other modules (the search box, the settings menu, the command bar) from basePath
  // when a shortcut asks for them. None is vendored, and none must ever be fetched: the loader
  // does nothing, so no key press makes a request.
  ace.config.setLoader(function () {});
  var holder = document.createElement("div");
  holder.className = "editor-box";
  area.parentNode.insertBefore(holder, area.nextSibling);
  // `useWorker` goes first: setting the mode with it still on would start a (blocked) worker.
  var editor = ace.edit(holder, {
    useWorker: false,
    theme: "ace/theme/textmate",
    showPrintMargin: false,
    showFoldWidgets: false,
    enableKeyboardAccessibility: true,
    tabSize: 2,
    useSoftTabs: true,
    wrap: false
  });
  editor.session.setMode("ace/mode/yaml");
  // The commands that need a module from basePath are removed, so the browser keeps its own
  // shortcuts (Ctrl+F finds in the page) instead of Ace swallowing them.
  ["find", "replace", "findnext", "findprevious", "showSettingsMenu", "openCommandPalette",
   "openCommandPallete", "showKeyboardShortcuts", "gotoline", "modeSelect"].forEach(function (name) {
    editor.commands.removeCommand(name);
  });
  editor.setValue(area.value, -1);
  editor.textInput.getElement().setAttribute("aria-label", "Definition (YAML)");
  area.hidden = true;
  // Before any button sends the form, the text area gets Ace's text.
  form.addEventListener("submit", function () {
    area.value = editor.getValue();
  });
})();
