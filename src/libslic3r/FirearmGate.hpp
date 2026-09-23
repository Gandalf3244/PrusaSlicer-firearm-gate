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

} // namespace Slic3r

#endif // slic3r_FirearmGate_hpp_
