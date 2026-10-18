///|/ Firearm-part gate: refuses to slice objects identified as printed-gun parts.
///|/
///|/ The identification itself is done by the external checker
///|/ (`python -m pipeline.check --json --units mm model.stl`, see
///|/ doc/Firearm gate.md). This module exports the plate geometry, runs the
///|/ checker and turns its verdict into a Print::validate() error string, which
///|/ is what disables the Slice / Export buttons in the GUI and makes the CLI
///|/ exit with an error.
#ifndef slic3r_FirearmGate_hpp_
#define slic3r_FirearmGate_hpp_

#include <functional>
#include <string>
#include <vector>

namespace Slic3r {

class ModelObject;

// The checker is found, in this order: the PRUSA_FIREARM_CHECK environment
// variable (an executable), a bundle at <resources>/firearm-check/ (private
// Python runtime in python/, the pipeline in app/ - what the installer ships),
// "firearm-check" on PATH, ~/.local/bin/firearm-check.
// PRUSA_FIREARM_CHECK_TIMEOUT: seconds to wait for one object (default 300).

// Returns an empty string when every object may be printed, otherwise a
// human readable refusal naming the objects and the evidence found.
// Fails closed: when the checker cannot be run, the refusal says so.
// Results are cached per object geometry and scale, so repeated calls
// (Print::validate() runs after every plate change) are free.
// The shape checked is the one that would be printed: model parts minus
// negative volumes and void modifiers (and, with `sla`, minus drain holes),
// mirrored parts wound the right way out.
std::string firearm_gate_validate(const std::vector<const ModelObject*> &objects, bool sla = false);

// GUI: check in the background instead of on the calling (UI) thread. Then
// firearm_gate_validate() never waits for the checker: an object without a
// verdict yet is refused as "being checked" and queued, and `on_checked` is
// called - from the worker thread - each time verdicts come in, so the
// caller can validate again. A failed check is shown for 30 s before the
// next validation checks it again.
void firearm_gate_set_background(std::function<void()> on_checked);

// Stops a background check still running (the checker process, the mesh
// reduction) and the worker thread. Call before the application quits.
void firearm_gate_shutdown();

// Starts the checker in the background (`--serve`), so its start-up - seconds
// on a slow machine - is over before the first object is checked. Optional:
// firearm_gate_validate() starts it on demand.
void firearm_gate_prestart();

} // namespace Slic3r

#endif // slic3r_FirearmGate_hpp_
