///|/ Firearm-part gate, see FirearmGate.hpp.
#include "FirearmGate.hpp"

#include "I18N.hpp"
#include "MeshBoolean.hpp"
#include "Model.hpp"
#include "QuadricEdgeCollapse.hpp"
#include "SLA/Hollowing.hpp"
#include "TriangleMesh.hpp"
#include "Utils.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstdlib>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <sstream>
#include <thread>
#include <unordered_map>
#include <unordered_set>

#include <boost/filesystem.hpp>
#include <boost/log/trivial.hpp>
#include <boost/nowide/cstdlib.hpp>
#include <boost/nowide/fstream.hpp>
#include <boost/process.hpp>
#ifndef _WIN32
#include <sys/wait.h>
#endif
#ifdef _WIN32
#include <boost/process/windows.hpp>
#endif
#include <nlohmann/json.hpp>

namespace Slic3r {

namespace {

namespace fs = boost::filesystem;
namespace bp = boost::process;

struct CheckResult
{
    // ALLOW / BLOCK verdict reached; false when the checker could not be run.
    bool        decided { false };
    bool        blocked { false };
    // Evidence lines (BLOCK) or the failure reason (! decided).
    std::string details;
};

// Results by object geometry + scale. Print::validate() runs after every
// plate change (move, rotate, reload...), the checker takes 0.1 s to tens of
// seconds, and the verdict only depends on the printed shape.
std::mutex                                   s_cache_mutex;
std::unordered_map<std::string, CheckResult> s_cache;
constexpr size_t                             s_cache_limit = 512;

// Set when the application quits: a check still running stops at the next
// poll of the checker or the next step of the mesh reduction.
std::atomic<bool> s_abort { false };
struct CheckCancelled {};

// How to start the checker: the executable, arguments that go before the
// gate's own, and the working directory it needs.
struct CheckerCommand
{
    fs::path                 exe;
    std::vector<std::string> args;
    fs::path                 cwd;
    bool                     empty() const { return exe.empty(); }
    std::string              describe() const { std::string s = exe.string(); for (const std::string &a : args) s += " " + a; return s; }
};

// Lookup order:
//   1. PRUSA_FIREARM_CHECK: an executable taking "--json --units mm <stl>".
//   2. A checker bundled with this build: <resources>/firearm-check/ holding a
//      private Python runtime (python/python.exe on Windows, python/bin/python3
//      elsewhere) and the pipeline in app/ - what the installer ships.
//   3. "firearm-check" on PATH, then ~/.local/bin/firearm-check (developer setups).
CheckerCommand checker_command()
{
    if (const char *env = boost::nowide::getenv("PRUSA_FIREARM_CHECK"); env != nullptr && *env != '\0')
        return { fs::path(env), {}, {} };

    if (! resources_dir().empty()) {
        const fs::path bundle = fs::path(resources_dir()) / "firearm-check";
#ifdef _WIN32
        const fs::path python = bundle / "python" / "python.exe";
#else
        const fs::path python = bundle / "python" / "bin" / "python3";
#endif
        boost::system::error_code ec;
        if (fs::exists(python, ec) && fs::exists(bundle / "app" / "pipeline" / "check.py", ec))
            return { python, { "-m", "pipeline.check" }, bundle / "app" };
    }

    if (fs::path p = bp::search_path("firearm-check"); ! p.empty())
        return { p, {}, {} };
    if (const char *home = boost::nowide::getenv("HOME"); home != nullptr && *home != '\0')
        if (fs::path p = fs::path(home) / ".local" / "bin" / "firearm-check"; fs::exists(p))
            return { p, {}, {} };
    return {};
}

int checker_timeout_seconds()
{
    if (const char *env = boost::nowide::getenv("PRUSA_FIREARM_CHECK_TIMEOUT"); env != nullptr && *env != '\0')
        if (int t = std::atoi(env); t > 0)
            return t;
    return 300;
}

// Largest mesh handed to the checker. Its memory grows ~2 KB per facet
// (trimesh caches, segmentation), so a 17M-facet object took 30+ GB and was
// killed by the OS - after minutes of the desktop swapping. No reference part
// has more than 820k facets: anything denser is reduced before the check.
size_t checker_max_facets()
{
    if (const char *env = boost::nowide::getenv("PRUSA_FIREARM_CHECK_MAX_FACETS"); env != nullptr && *env != '\0')
        if (long long n = std::atoll(env); n >= 10000)
            return size_t(n);
    return 1000000;
}

// Quadric edge collapse (the Simplify tool's) down to checker_max_facets():
// the facets of a dense mesh go first where they add no shape - flat areas,
// along the axis of a finely tessellated cylinder - so the holes and bosses
// the checker measures keep their diameters.
void reduce_for_checker(TriangleMesh &mesh, const std::string &name, const char *what)
{
    const size_t limit = checker_max_facets();
    if (mesh.facets_count() <= limit)
        return;
    const size_t before = mesh.facets_count();
    const auto   t0     = std::chrono::steady_clock::now();
    indexed_triangle_set its = std::move(mesh.its);
    its_quadric_edge_collapse(its, uint32_t(limit), nullptr, []() { if (s_abort) throw CheckCancelled(); });
    mesh = TriangleMesh(std::move(its));
    BOOST_LOG_TRIVIAL(info) << "Firearm gate: \"" << name << "\": " << what << " reduced from " << before << " to "
                            << mesh.facets_count() << " facets in "
                            << std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count() << " s";
}

// Effective value of a numeric region option inside a modifier: its own
// setting, else the object's; unknown when neither sets it.
std::optional<double> numeric_value(const ConfigOption *opt)
{
    if (opt == nullptr)
        return std::nullopt;
    switch (opt->type()) {
    case coInt:     return double(opt->getInt());      // getFloat() throws on an int option
    case coFloat:
    case coPercent: return opt->getFloat();
    default:        return std::nullopt;
    }
}

std::optional<double> region_option(const ModelVolume &v, const ModelObject &object, const char *key)
{
    if (std::optional<double> own = numeric_value(v.config.option(key)))
        return own;
    return numeric_value(object.config.option(key));
}

// A modifier that prints nothing inside itself (no perimeters, 0 % infill)
// leaves a void where it overlaps the part - it cuts like a negative volume.
bool is_void_modifier(const ModelVolume &v, const ModelObject &object)
{
    if (! v.is_modifier())
        return false;
    const std::optional<double> perimeters = region_option(v, object, "perimeters");
    const std::optional<double> infill     = region_option(v, object, "fill_density");
    return perimeters && infill && *perimeters == 0. && *infill == 0.;
}

// What one check needs from the model, copied on the thread that owns the
// model: the meshes are shared and immutable, so a check running in the
// background reads them while the user goes on editing the plate.
struct CheckInput
{
    struct Volume { std::shared_ptr<const TriangleMesh> mesh; Transform3d matrix; bool part; };
    std::string              key;
    std::string              name;
    std::vector<Volume>      volumes;      // model parts and what cuts them
    std::vector<sla::DrainHole> drain_holes;  // SLA only
    Transform3d              instance;     // its matrix without the offset
    size_t                   facets() const { size_t n = 0; for (const Volume &v : volumes) n += v.mesh->facets_count(); return n; }
};

CheckInput check_input(const ModelObject &object, const ModelInstance &instance, bool sla, std::string key)
{
    CheckInput in;
    in.key      = std::move(key);
    in.name     = object.name;
    in.instance = instance.get_matrix_no_offset();
    for (const ModelVolume *v : object.volumes)
        if (v->is_model_part() || v->is_negative_volume() || is_void_modifier(*v, object))
            in.volumes.push_back({ v->get_mesh_shared_ptr(), v->get_matrix(), v->is_model_part() });
    if (sla)
        for (const sla::DrainHole &hole : object.sla_drain_holes)
            if (! hole.failed)
                in.drain_holes.emplace_back(hole);
    return in;
}

// The shape that would be printed, in object coordinates (instance transform
// not applied): model parts minus negative volumes, void modifiers and (SLA)
// drain holes. Mirrored volumes are re-wound so they stay right way out -
// an inside-out copy reads every hole as a pin.
TriangleMesh printed_mesh(const CheckInput &object)
{
    TriangleMesh parts, cut;
    for (const CheckInput::Volume &v : object.volumes) {
        TriangleMesh m(*v.mesh);
        m.transform(v.matrix, true);
        (v.part ? parts : cut).merge(m);
    }
    BOOST_LOG_TRIVIAL(debug) << "Firearm gate: \"" << object.name << "\": " << object.volumes.size() << " volumes, "
                             << parts.facets_count() << " part facets, " << cut.facets_count() << " cut facets"
                             << "; parts box " << parts.bounding_box().min.transpose() << " / " << parts.bounding_box().max.transpose()
                             << "; cut box " << cut.bounding_box().min.transpose() << " / " << cut.bounding_box().max.transpose();
    for (const sla::DrainHole &hole : object.drain_holes)
        cut.merge(TriangleMesh(hole.to_mesh()));
    // before the boolean as well: CGAL on a dense mesh is slower and hungrier still
    reduce_for_checker(parts, object.name, "parts");
    reduce_for_checker(cut, object.name, "cut volumes");
    if (! cut.empty() && ! parts.empty()) {
        const size_t n_parts = parts.facets_count(), n_cut = cut.facets_count();
        try {
            TriangleMesh result = parts;
            MeshBoolean::cgal::minus(result, cut);
            parts = std::move(result);
            BOOST_LOG_TRIVIAL(info) << "Firearm gate: \"" << object.name << "\": " << n_parts << " part facets minus "
                                    << n_cut << " cut facets -> " << parts.facets_count();
        } catch (...) {
            // Open or self-intersecting meshes cannot be subtracted. The removed
            // volumes turned inside out are the walls of the holes they cut, which
            // is what the checker measures.
            cut.flip_triangles();
            parts.merge(cut);
            BOOST_LOG_TRIVIAL(info) << "Firearm gate: \"" << object.name << "\": boolean failed, " << n_cut
                                    << " cut facets added inside out";
        }
        reduce_for_checker(parts, object.name, "printed shape");
    }
    return parts;
}

// What an instance does to the object's shape, independent of where it is
// placed: M^T M of the linear part (rotation-free; scaling, skew and their
// axes) and whether it mirrors. Moving or rotating an object on the plate keeps
// the cached verdict; scaling, skewing or mirroring it does not.
std::string instance_shape(const ModelInstance &instance)
{
    const Matrix3d m   = instance.get_matrix_no_offset().matrix().block<3, 3>(0, 0);
    const Matrix3d mtm = m.transpose() * m;
    std::ostringstream ss;
    ss.precision(6);
    for (int r = 0; r < 3; ++ r)
        for (int c = r; c < 3; ++ c)
            ss << mtm(r, c) << ',';
    ss << (m.determinant() < 0. ? "mirrored" : "direct");
    return ss.str();
}

// Every volume that shapes the print is part of the key (parts, negative
// volumes, modifiers with the two options that can make them void), and so
// are the SLA drain holes and the instance shape.
std::string cache_key(const ModelObject &object, const std::string &shape, bool sla)
{
    std::ostringstream ss;
    ss.precision(6);
    for (const ModelVolume *v : object.volumes)
        if (v->is_model_part() || v->is_negative_volume() || v->is_modifier()) {
            // The mesh is shared and immutable; the counts and the box guard against
            // an address being reused by a different mesh.
            const TriangleMesh &m  = v->mesh();
            const BoundingBoxf3 bb = m.bounding_box();
            ss << int(v->type()) << ':' << v->get_mesh_shared_ptr().get() << ':' << m.facets_count() << ':'
               << m.its.vertices.size() << ':' << bb.min.transpose() << ':' << bb.max.transpose() << ':';
            const Transform3d &t = v->get_matrix();
            for (int r = 0; r < 3; ++ r)
                for (int c = 0; c < 4; ++ c)
                    ss << t(r, c) << ',';
            if (v->is_modifier())
                ss << "void=" << is_void_modifier(*v, object);
            ss << ';';
        }
    if (sla) {
        ss << "sla";
        for (const sla::DrainHole &h : object.sla_drain_holes)
            ss << ';' << h.pos.transpose() << ',' << h.normal.transpose() << ',' << h.radius << ',' << h.height << ',' << h.failed;
    }
    ss << ";shape=" << shape;
    return ss.str();
}

struct TempFile
{
    fs::path path;
    explicit TempFile(const char *pattern) : path(fs::temp_directory_path() / fs::unique_path(pattern)) {}
    ~TempFile() { boost::system::error_code ec; fs::remove(path, ec); }
};

std::string read_file(const fs::path &path)
{
    boost::nowide::ifstream ifs(path.string());
    std::stringstream ss;
    ss << ifs.rdbuf();
    return ss.str();
}

// Evidence lines of one BLOCK verdict, or the reason an input was not decided.
CheckResult result_from_json(const nlohmann::json &verdict)
{
    CheckResult result;
    const std::string v = verdict.value("verdict", std::string());
    if (v == "ALLOW") {
        result.decided = true;
        return result;
    }
    if (v != "BLOCK") {
        result.details = _u8L("the checker could not read the geometry") + ": " + verdict.value("error", std::string());
        return result;
    }
    result.decided = true;
    result.blocked = true;
    std::ostringstream ss;
    for (const nlohmann::json &e : verdict.value("evidence", nlohmann::json::array())) {
        std::string level = e.value("level", std::string());
        level.resize(9, ' ');
        ss << "    " << level << e.value("what", std::string());
        if (const std::string body = e.value("body", std::string()); ! body.empty() && body != "whole")
            ss << "  [" << body << "]";
        ss << '\n';
    }
    result.details = ss.str();
    return result;
}

// Verdict lines (one JSON object per line) matched to the inputs by file name:
// the names are unique and random, and the checker may normalise the directory
// part differently. Inputs without a line get `missing` as the failure reason.
void parse_verdicts(const std::string &text, const std::vector<std::unique_ptr<TempFile>> &stls,
                    std::vector<CheckResult> &results, const std::string &missing)
{
    std::unordered_map<std::string, size_t> index;
    for (size_t i = 0; i < stls.size(); ++ i)
        index[stls[i]->path.filename().string()] = i;
    std::vector<bool> answered(results.size(), false);
    std::istringstream lines(text);
    for (std::string line; std::getline(lines, line); ) {
        if (line.find('{') == std::string::npos)
            continue;
        try {
            const nlohmann::json verdict = nlohmann::json::parse(line);
            if (auto it = index.find(fs::path(verdict.value("path", std::string())).filename().string()); it != index.end()) {
                results[it->second]  = result_from_json(verdict);
                answered[it->second] = true;
            }
        } catch (const std::exception &) {
        }
    }
    for (size_t i = 0; i < results.size(); ++ i)
        if (! answered[i])
            results[i].details = missing;
}

std::string tail(std::string text, size_t n = 600)
{
    return text.size() > n ? "..." + text.substr(text.size() - n) : text;
}

// The checker kept running between checks (`--serve`): Python, numpy/scipy and
// the reference library load once per session instead of once per check -
// seconds on a slow laptop, where Windows also scans every DLL Python loads.
// Any problem with it falls back to a one-shot run, so correctness never
// depends on the server.
struct CheckerServer
{
    std::mutex                     mutex;
    std::string                    command;        // CheckerCommand::describe() it runs
    std::string                    unsupported;    // a command whose server exited at once (no --serve)
    std::unique_ptr<bp::opstream>  in;
    std::unique_ptr<bp::child>     child;
    std::unique_ptr<TempFile>      err;
    bool                           answered_once { false };

    ~CheckerServer() { stop(); }
    void stop()
    {
        if (in) {
            in->close();                           // end of input: the server exits by itself
            in.reset();
        }
        if (child) {
            boost::system::error_code ec;
            for (int i = 0; i < 50 && child->running(ec); ++ i)
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
            if (child->running(ec))
                child->terminate(ec);
            child.reset();
        }
        err.reset();
        answered_once = false;
    }
    bool running() { boost::system::error_code ec; return child && child->running(ec); }
    bool start(const CheckerCommand &checker)
    {
        stop();
#ifndef _WIN32
        // a write to a server that just died must not kill the slicer with SIGPIPE
        std::signal(SIGPIPE, SIG_IGN);
#endif
        try {
            err = std::make_unique<TempFile>("prusaslicer-firearm-server-%%%%%%%%.err");
            in  = std::make_unique<bp::opstream>();
            std::vector<std::string> args = checker.args;
            for (const char *a : { "--serve", "--units", "mm" })
                args.emplace_back(a);
            const fs::path cwd = checker.cwd.empty() ? fs::current_path() : checker.cwd;
            child = std::make_unique<bp::child>(checker.exe.string(), bp::args(args), bp::start_dir(cwd.string()),
                                                bp::std_in < *in, bp::std_out > bp::null, bp::std_err > err->path.string()
#ifdef _WIN32
                                                , bp::windows::create_no_window
#endif
                                                );
            command = checker.describe();
            return true;
        } catch (const std::exception &ex) {
            BOOST_LOG_TRIVIAL(warning) << "Firearm gate: checker server did not start: " << ex.what();
            stop();
            return false;
        }
    }
};
CheckerServer s_server;

// Killed: the OS stopped the server mid-check (out of memory); a one-shot run
// of the same input would only be killed again, after as long a wait.
enum class ServerOutcome { Answered, Unavailable, TimedOut, Killed };

// Whether the child that just exited was killed with SIGKILL - what the Linux
// OOM killer sends. (A Python on Windows out of memory raises MemoryError,
// which the checker reports as an ERROR verdict instead.)
bool killed_by_sigkill(bp::child &child)
{
#ifdef _WIN32
    (void)child;
    return false;
#else
    boost::system::error_code ec;
    child.wait(ec);
    const int status = child.native_exit_code();
    return WIFSIGNALED(status) && WTERMSIG(status) == SIGKILL;
#endif
}

// One request to the server. Unavailable: use a one-shot run instead.
ServerOutcome server_check(const CheckerCommand &checker, const std::vector<std::unique_ptr<TempFile>> &stls,
                           int timeout, std::string &text, std::string &why)
{
    std::lock_guard<std::mutex> lock(s_server.mutex);
    const std::string command = checker.describe();
    if (s_server.unsupported == command)
        return ServerOutcome::Unavailable;
    if (! s_server.running() || s_server.command != command)
        if (! s_server.start(checker))
            return ServerOutcome::Unavailable;

    TempFile out("prusaslicer-firearm-%%%%%%%%.out");
    TempFile done("prusaslicer-firearm-%%%%%%%%.done");
    nlohmann::json request;
    request["files"] = nlohmann::json::array();
    for (const auto &stl : stls)
        request["files"].push_back(stl->path.string());
    request["out"]  = out.path.string();
    request["done"] = done.path.string();
    // ASCII only (non-ASCII escaped), whatever code page Python reads stdin with
    *s_server.in << request.dump(-1, ' ', true) << std::endl;
    if (! *s_server.in) {
        s_server.stop();
        return ServerOutcome::Unavailable;
    }

    const auto start    = std::chrono::steady_clock::now();
    const auto deadline = start + std::chrono::seconds(timeout);
    boost::system::error_code ec;
    while (! fs::exists(done.path, ec)) {
        if (! s_server.running()) {
            // exited without answering: a checker without --serve exits at once
            // (argument error), a crash on some input exits later
            const bool at_once = ! s_server.answered_once &&
                                 std::chrono::steady_clock::now() - start < std::chrono::seconds(20);
            why = tail(read_file(s_server.err->path));
            if (at_once)
                s_server.unsupported = command;
            const bool killed = ! at_once && killed_by_sigkill(*s_server.child);
            s_server.stop();
            if (killed) {
                why = _u8L("the checker was killed by the system while checking (out of memory?)");
                return ServerOutcome::Killed;
            }
            return ServerOutcome::Unavailable;
        }
        if (std::chrono::steady_clock::now() > deadline) {
            s_server.stop();
            why = _u8L("the checker did not finish in") + " " + std::to_string(timeout) + " s";
            return ServerOutcome::TimedOut;
        }
        if (s_abort) {
            s_server.stop();
            throw CheckCancelled();
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    s_server.answered_once = true;
    text = read_file(out.path);
    return ServerOutcome::Answered;
}

// Several meshes, one check: through the server when it works, else one
// checker process for all of them (`--json` prints one line per input).
std::vector<CheckResult> run_checker(const CheckerCommand &checker, const std::vector<TriangleMesh> &meshes,
                                     const std::vector<std::string> &names)
{
    std::vector<CheckResult> results(meshes.size());
    std::vector<std::unique_ptr<TempFile>> stls;
    for (size_t i = 0; i < meshes.size(); ++ i) {
        stls.emplace_back(std::make_unique<TempFile>("prusaslicer-firearm-%%%%%%%%.stl"));
        if (! its_write_stl_binary(stls.back()->path.string().c_str(), names[i].c_str(), meshes[i].its)) {
            for (CheckResult &r : results)
                r.details = _u8L("could not write the temporary STL");
            return results;
        }
    }
    // PRUSA_FIREARM_CHECK_TIMEOUT is per object
    const int timeout = checker_timeout_seconds() * int(std::max<size_t>(1, meshes.size()));

    std::string text, why;
    switch (server_check(checker, stls, timeout, text, why)) {
    case ServerOutcome::Answered:
        parse_verdicts(text, stls, results, _u8L("the checker did not return a verdict") + "\n" + why);
        return results;
    case ServerOutcome::TimedOut:
    case ServerOutcome::Killed:
        for (CheckResult &r : results)
            r.details = why;
        return results;
    case ServerOutcome::Unavailable:
        if (! why.empty())
            BOOST_LOG_TRIVIAL(info) << "Firearm gate: checker server unavailable, one-shot run: " << why;
        break;
    }

    TempFile out("prusaslicer-firearm-%%%%%%%%.out");
    TempFile err("prusaslicer-firearm-%%%%%%%%.err");
    int exit_code = -1;
    auto fail_all = [&results](const std::string &why) {
        for (CheckResult &r : results)
            if (! r.decided)
                r.details = why;
        return results;
    };
    try {
        std::vector<std::string> args = checker.args;
        for (const char *a : { "--json", "--units", "mm" })
            args.emplace_back(a);
        for (const auto &stl : stls)
            args.emplace_back(stl->path.string());
        const fs::path cwd = checker.cwd.empty() ? fs::current_path() : checker.cwd;
        bp::child child(checker.exe.string(), bp::args(args), bp::start_dir(cwd.string()),
                        bp::std_out > out.path.string(), bp::std_err > err.path.string(), bp::std_in < bp::null
#ifdef _WIN32
                        , bp::windows::create_no_window   // python.exe must not flash a console under the GUI
#endif
                        );
        // Polled rather than wait_for(): Boost marks wait_for unreliable.
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout);
        while (child.running()) {
            if (std::chrono::steady_clock::now() > deadline) {
                child.terminate();
                return fail_all(_u8L("the checker did not finish in") + " " + std::to_string(timeout) + " s");
            }
            if (s_abort) {
                child.terminate();
                throw CheckCancelled();
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
        child.wait();
        exit_code = child.exit_code();
    } catch (const std::exception &ex) {
        return fail_all(std::string(_u8L("could not start the checker")) + ": " + ex.what());
    }

    parse_verdicts(read_file(out.path), stls, results,
                   _u8L("the checker did not return a verdict") + " (" + _u8L("exit code") + " "
                   + std::to_string(exit_code) + ")\n" + tail(read_file(err.path)));
    return results;
}

// Checks the inputs on the calling thread and caches the decided verdicts.
// Returns every verdict by key; failures are returned, not cached, so fixing
// the checker takes effect at the next check.
std::unordered_map<std::string, CheckResult> check_inputs(const CheckerCommand &checker, const std::vector<CheckInput> &inputs)
{
    std::unordered_map<std::string, CheckResult> found;
    std::vector<TriangleMesh>      meshes;
    std::vector<std::string>       names;
    std::vector<const CheckInput*> sent;
    for (const CheckInput &in : inputs) {
        // the printed shape, then the instance scaling / skew / mirroring
        // (re-wound when mirrored, see printed_mesh)
        TriangleMesh mesh = printed_mesh(in);
        mesh.transform(in.instance, true);
        if (mesh.empty()) {
            // everything cut away by negative volumes: nothing is printed
            CheckResult nothing;
            nothing.decided = true;
            found[in.key] = nothing;
            continue;
        }
        BOOST_LOG_TRIVIAL(info) << "Firearm gate: checking \"" << in.name << "\" (" << mesh.facets_count() << " facets)";
        meshes.emplace_back(std::move(mesh));
        names.emplace_back(in.name);
        sent.emplace_back(&in);
    }
    std::vector<CheckResult> results;
    if (! sent.empty()) {
        const auto t0 = std::chrono::steady_clock::now();
        results = run_checker(checker, meshes, names);
        BOOST_LOG_TRIVIAL(info) << "Firearm gate: " << sent.size() << " object(s) checked in "
                                << std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count() << " s";
    }
    std::lock_guard<std::mutex> lock(s_cache_mutex);
    for (size_t i = 0; i < sent.size(); ++ i) {
        const CheckResult &r = results[i];
        BOOST_LOG_TRIVIAL(info) << "Firearm gate: \"" << sent[i]->name << "\" -> "
                                << (! r.decided ? "FAILED" : r.blocked ? "BLOCK" : "ALLOW");
        found[sent[i]->key] = r;
    }
    for (const auto &[key, r] : found)
        if (r.decided) {
            if (s_cache.size() >= s_cache_limit)
                s_cache.clear();
            s_cache[key] = r;
        }
    return found;
}

// Checking in the background (the GUI): reducing and checking a dense mesh
// takes up to minutes, which on the UI thread froze the window. There,
// firearm_gate_validate() never waits: it queues what has no verdict yet and
// refuses until it has one; the worker then calls `on_checked`, on which the
// GUI validates the plate again.
struct Background
{
    using Clock = std::chrono::steady_clock;
    std::mutex                      mutex;
    std::condition_variable         cv;
    std::function<void()>           on_checked;
    std::vector<CheckInput>         queue;
    std::unordered_set<std::string> in_flight;   // queued or being checked
    // Failures are shown for a while before the next validation checks again:
    // validating right after a failure must not just queue the same check.
    std::unordered_map<std::string, std::pair<CheckResult, Clock::time_point>> failed;
    std::thread                     worker;
    bool                            stop { false };

    ~Background() { shutdown(); }
    void shutdown()
    {
        s_abort = true;
        std::thread w;
        {
            std::lock_guard<std::mutex> lock(mutex);
            stop       = true;
            on_checked = nullptr;
            w          = std::move(worker);
        }
        cv.notify_all();
        if (w.joinable())
            w.join();
    }
    void run();
};
Background s_background;
constexpr auto s_failure_shown = std::chrono::seconds(30);

void Background::run()
{
    for (;;) {
        std::vector<CheckInput> batch;
        {
            std::unique_lock<std::mutex> lock(mutex);
            cv.wait(lock, [this] { return stop || ! queue.empty(); });
            if (stop)
                return;
            batch.swap(queue);
        }
        std::unordered_map<std::string, CheckResult> found;
        std::string why;
        try {
            if (const CheckerCommand checker = checker_command(); checker.empty())
                why = _u8L("the firearm-part checker was not found");
            else
                found = check_inputs(checker, batch);
        } catch (const CheckCancelled &) {
            return;
        } catch (const std::exception &ex) {
            // std::bad_alloc on a mesh too large to copy: refuse instead of taking the application down
            why = _u8L("the check failed") + ": " + ex.what();
        }
        std::function<void()> notify;
        {
            std::lock_guard<std::mutex> lock(mutex);
            const Clock::time_point now = Clock::now();
            for (const CheckInput &in : batch) {
                CheckResult r;
                if (auto it = found.find(in.key); it != found.end())
                    r = it->second;
                else
                    r.details = why;
                if (r.decided)
                    failed.erase(in.key);
                else
                    failed[in.key] = { r, now };
                in_flight.erase(in.key);
            }
            notify = on_checked;
        }
        if (notify)
            notify();
    }
}

} // namespace

void firearm_gate_set_background(std::function<void()> on_checked)
{
    std::lock_guard<std::mutex> lock(s_background.mutex);
    s_background.on_checked = std::move(on_checked);
}

void firearm_gate_shutdown()
{
    s_background.shutdown();
}

void firearm_gate_prestart()
{
    const CheckerCommand checker = checker_command();
    if (checker.empty())
        return;
    std::lock_guard<std::mutex> lock(s_server.mutex);
    if (! s_server.running() && s_server.unsupported != checker.describe())
        s_server.start(checker);
}

std::string firearm_gate_validate(const std::vector<const ModelObject*> &objects, bool sla)
{
    if (objects.empty())
        return {};

    if (s_abort)
        return _u8L("Slicing is disabled: PrusaSlicer is closing.");

    const CheckerCommand checker = checker_command();
    if (checker.empty())
        return _u8L("Slicing is disabled: the firearm-part checker was not found.") + "\n"
             + _u8L("Set PRUSA_FIREARM_CHECK to the checker executable or put \"firearm-check\" on PATH.");

    // One check per object and distinct instance shape (most objects have one);
    // cached verdicts are reused, the rest go to the checker in a single run.
    struct Item { const ModelObject *object; const ModelInstance *instance; std::string key; };
    std::vector<Item>                   items;
    std::unordered_map<std::string, CheckResult> found;
    for (const ModelObject *object : objects) {
        if (object == nullptr || object->instances.empty())
            continue;
        std::vector<std::string> shapes;
        for (const ModelInstance *instance : object->instances) {
            std::string shape = instance_shape(*instance);
            if (std::find(shapes.begin(), shapes.end(), shape) != shapes.end())
                continue;
            shapes.emplace_back(shape);
            items.push_back({ object, instance, cache_key(*object, shape, sla) });
        }
    }
    {
        std::lock_guard<std::mutex> lock(s_cache_mutex);
        for (const Item &item : items)
            if (auto it = s_cache.find(item.key); it != s_cache.end())
                found[item.key] = it->second;
    }
    // The rest is checked here (command line), or queued for the background
    // worker (GUI) unless it is already queued, being checked, or just failed.
    std::vector<CheckInput> inputs;
    {
        std::lock_guard<std::mutex> lock(s_background.mutex);
        const bool background = bool(s_background.on_checked);
        bool       queued     = false;
        for (const Item &item : items) {
            if (found.find(item.key) != found.end() ||
                std::any_of(inputs.begin(), inputs.end(), [&item](const CheckInput &in) { return in.key == item.key; }))
                continue;
            if (background) {
                if (auto it = s_background.failed.find(item.key);
                    it != s_background.failed.end() && Background::Clock::now() - it->second.second < s_failure_shown) {
                    found[item.key] = it->second.first;
                    continue;
                }
                if (! s_background.in_flight.insert(item.key).second)
                    continue;
                s_background.queue.emplace_back(check_input(*item.object, *item.instance, sla, item.key));
                queued = true;
            } else
                inputs.emplace_back(check_input(*item.object, *item.instance, sla, item.key));
        }
        if (queued) {
            if (! s_background.worker.joinable())
                s_background.worker = std::thread([] { s_background.run(); });
            s_background.cv.notify_one();
        }
    }
    if (! inputs.empty())
        for (auto &[key, r] : check_inputs(checker, inputs))
            found[key] = std::move(r);

    std::ostringstream blocked, failed, checking;
    std::vector<const ModelObject*> reported;
    for (const Item &item : items) {
        if (std::find(reported.begin(), reported.end(), item.object) != reported.end())
            continue;
        auto it = found.find(item.key);
        if (it == found.end()) {
            // in the background check
            checking << "  \"" << item.object->name << "\"";
            size_t facets = 0;
            for (const ModelVolume *v : item.object->volumes)
                if (v->is_model_part())
                    facets += v->mesh().facets_count();
            if (facets > checker_max_facets())
                checking << " (" << facets << " " << _u8L("facets; reducing a mesh this dense for the check takes a while") << ")";
            checking << '\n';
            reported.emplace_back(item.object);
        } else if (const CheckResult &r = it->second; ! r.decided) {
            failed << "  \"" << item.object->name << "\": " << r.details << '\n';
            reported.emplace_back(item.object);
        } else if (r.blocked) {
            blocked << "  \"" << item.object->name << "\"\n" << r.details;
            reported.emplace_back(item.object);
        }
    }

    std::string message;
    if (! blocked.str().empty())
        message += _u8L("Slicing refused: the plate contains a firearm part.") + "\n"
                 + _u8L("The following object matches the reference library of printed-gun parts "
                        "(design = identical part, lineage = derivative of a known design, "
                        "platform = firearm-family interface, bore = barrel at a bullet diameter):") + "\n"
                 + blocked.str()
                 + _u8L("Remove it from the plate to continue.");
    if (! failed.str().empty()) {
        if (! message.empty())
            message += "\n";
        message += _u8L("Slicing is disabled: the firearm-part check could not be completed.") + "\n" + failed.str();
    }
    if (! checking.str().empty()) {
        if (! message.empty())
            message += "\n";
        message += _u8L("Slicing is disabled until the firearm-part check is done. Checking:") + "\n" + checking.str();
    }
    return message;
}

} // namespace Slic3r
