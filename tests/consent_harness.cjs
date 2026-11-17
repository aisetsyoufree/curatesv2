// Execute the shipped script with inert DOM objects. Never fetch or run Google code.
const fs = require("node:fs");
const vm = require("node:vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const requests = [];
const context = {
  location: { origin: "https://cu-rates.org", pathname: "/compare", search: "?private=secret" },
  document: {
    querySelector: () => ({ dataset: input.dataset }),
    createElement: (type) => {
      if (type !== "script") throw new Error("unexpected resource");
      return {};
    },
    head: { appendChild: (tag) => requests.push(tag.src) },
    set cookie(value) { throw new Error("script wrote a cookie: " + value); },
  },
};
context.window = context;
vm.runInNewContext(input.source, context, { timeout: 1000 });
process.stdout.write(JSON.stringify({
  requests,
  calls: Array.from(context.dataLayer, (args) => Array.from(args)),
}));
