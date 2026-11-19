// The copy button of the test bench: it puts the SQL text on the clipboard. The page works
// without this file (the SQL is plain, selectable text), and the button stays hidden until the
// script has run. It reads the page's own text and calls no other address.
(function () {
  "use strict";
  var button = document.getElementById("copy-sql");
  var block = document.getElementById("sql-text");
  if (!button || !block) {
    return;
  }
  button.hidden = false;
  button.addEventListener("click", function () {
    var done = function () {
      button.textContent = "Copied";
    };
    var failed = function () {
      button.textContent = "Select the SQL and copy it";
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(block.textContent).then(done, failed);
    } else {
      failed();
    }
  });
})();
