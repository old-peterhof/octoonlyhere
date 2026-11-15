/* Settings page for PrintPocket inside OctoPrint. */
$(function () {
  function PrintPocketSettingsViewModel(parameters) {
    var self = this;
    self.settingsViewModel = parameters[0];
    self.settings = undefined;

    self.appUrl = ko.observable(location.origin + OctoPrint.getBaseUrl().replace(/^https?:\/\/[^/]+/, "") + "plugin/printpocket/");
    self.code = ko.observable("");
    self.codePretty = ko.pureComputed(function () {
      var c = self.code();
      return c ? c.slice(0, 3) + " " + c.slice(3) : "";
    });
    self.pairing = ko.observable(false);
    self.testing = ko.observable(false);
    self.testResult = ko.observable("");
    self.events = [
      { key: "started", label: "Print started" },
      { key: "done", label: "Print finished" },
      { key: "failed", label: "Print failed" },
      { key: "cancelled", label: "Print cancelled" },
      { key: "paused", label: "Print paused" },
      { key: "filament", label: "Filament change (M600)" },
      { key: "progress", label: "Progress updates" },
      { key: "error", label: "Printer errors" }
    ];

    var codeTimer = null;

    self.onBeforeBinding = function () {
      self.settings = self.settingsViewModel.settings;
    };

    self.newCode = function () {
      self.pairing(true);
      OctoPrint.postJson("plugin/printpocket/api/pair", {})
        .done(function (r) {
          self.code(r.code);
          clearTimeout(codeTimer);
          codeTimer = setTimeout(function () { self.code(""); }, r.expiresIn * 1000);
        })
        .fail(function () {
          new PNotify({ title: "PrintPocket", text: "Couldn't create a pairing code.", type: "error" });
        })
        .always(function () { self.pairing(false); });
    };

    self.newTopic = function () {
      var current = self.settings.plugins.printpocket.ntfy_url() || "";
      var m = current.match(/^(https?:\/\/[^/]+)/);
      OctoPrint.get("plugin/printpocket/api/ntfy/topic", { data: { server: m ? m[1] : "https://ntfy.sh" } })
        .done(function (r) { self.settings.plugins.printpocket.ntfy_url(r.url); });
    };

    self.testNtfy = function () {
      self.testing(true);
      self.testResult("");
      OctoPrint.postJson("plugin/printpocket/api/ntfy/test", {})
        .done(function () { self.testResult("Sent. Check your phone."); })
        .fail(function (xhr) {
          var msg = (xhr.responseJSON && xhr.responseJSON.error) || "Failed";
          self.testResult(msg);
        })
        .always(function () { self.testing(false); });
    };
  }

  OCTOPRINT_VIEWMODELS.push({
    construct: PrintPocketSettingsViewModel,
    dependencies: ["settingsViewModel"],
    elements: ["#printpocket_settings"]
  });
});
