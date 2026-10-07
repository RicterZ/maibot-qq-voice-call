"use strict";

// Native Windows 9.9.31: 64/65 enumerate device names; 102 selects input/output.
const ALLOWED_COMMANDS = new Set([1, 5, 55, 64, 65, 102]);
function validInvocation(command, params) {
  if (!ALLOWED_COMMANDS.has(command) || !Array.isArray(params) || params.length > 16) return false;
  if (command === 64 || command === 65) return params.length === 0;
  if (command === 102) {
    return params.length === 2 && params.every((value) =>
      Number.isInteger(value) && value > 0 && value < 0xffffffff);
  }
  return true;
}
module.exports = { validInvocation };
