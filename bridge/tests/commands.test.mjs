import assert from "node:assert/strict";
import test from "node:test";
import protocol from "../av-host/commands.cjs";

test("native device enumeration takes no arguments", () => {
  for (const command of [64, 65]) {
    assert.equal(protocol.validInvocation(command, []), true);
    assert.equal(protocol.validInvocation(command, [0]), false);
  }
});
test("native device selection requires two explicit unsigned selectors", () => {
  assert.equal(protocol.validInvocation(102, [1, 3]), true);
  for (const params of [[], [0, 1], [1, 0], [0, 0], [1], [1, 2, 3], [-1, 0], [0xffffffff, 0], [1.5, 0], ["1", 0], [null, 0]]) {
    assert.equal(protocol.validInvocation(102, params), false);
  }
  assert.equal(protocol.validInvocation(999, []), false);
});
