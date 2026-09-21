///|/ Firearm-part gate, see FirearmGate.hpp.
#include "FirearmGate.hpp"

#include "I18N.hpp"
#include "Model.hpp"
#include "TriangleMesh.hpp"
#include "Utils.hpp"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <mutex>
#include <sstream>
#include <thread>
#include <unordered_map>

#include <boost/filesystem.hpp>
#include <boost/log/trivial.hpp>
#include <boost/nowide/cstdlib.hpp>
#include <boost/nowide/fstream.hpp>
#include <boost/process.hpp>
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

// The identification is pose independent, so an instance is characterised by
// its scaling and mirroring only: moving or rotating an object on the plate
// keeps the cached verdict, scaling it does not.
std::string cache_key(const ModelObject &object, const Vec3d &scale, const Vec3d &mirror)
{
    std::ostringstream ss;
    ss.precision(6);
    for (const ModelVolume *v : object.volumes)
        if (v->is_model_part()) {
            // The mesh is shared and immutable; the counts and the box guard against
            // an address being reused by a different mesh.
            const TriangleMesh &m  = v->mesh();
            const BoundingBoxf3 bb = m.bounding_box();
            ss << v->get_mesh_shared_ptr().get() << ':' << m.facets_count() << ':' << m.its.vertices.size()
               << ':' << bb.min.transpose() << ':' << bb.max.transpose() << ':';
            const Transform3d &t = v->get_matrix();
            for (int r = 0; r < 3; ++ r)
                for (int c = 0; c < 4; ++ c)
                    ss << t(r, c) << ',';
            ss << ';';
        }
    ss << "scale=" << scale.transpose() << ";mirror=" << mirror.transpose();
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

CheckResult run_checker(const CheckerCommand &checker, const TriangleMesh &mesh, const std::string &name)
{
    CheckResult result;
    TempFile stl("prusaslicer-firearm-%%%%%%%%.stl");
    TempFile out("prusaslicer-firearm-%%%%%%%%.out");
    TempFile err("prusaslicer-firearm-%%%%%%%%.err");

    if (! its_write_stl_binary(stl.path.string().c_str(), name.c_str(), mesh.its)) {
        result.details = _u8L("could not write the temporary STL");
        return result;
    }

    const int timeout = checker_timeout_seconds();
    int exit_code = -1;
    try {
        std::vector<std::string> args = checker.args;
        for (const char *a : { "--json", "--units", "mm" })
            args.emplace_back(a);
        args.emplace_back(stl.path.string());
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
                result.details = _u8L("the checker did not finish in") + " " + std::to_string(timeout) + " s";
                return result;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
        child.wait();
        exit_code = child.exit_code();
    } catch (const std::exception &ex) {
        result.details = std::string(_u8L("could not start the checker")) + ": " + ex.what();
        return result;
    }

    const std::string stdout_text = read_file(out.path);
    // `--json` prints one object per input file; we pass exactly one.
    nlohmann::json verdict;
    try {
        verdict = nlohmann::json::parse(stdout_text);
    } catch (const std::exception &) {
        std::string stderr_text = read_file(err.path);
        if (stderr_text.size() > 600)
            stderr_text = "..." + stderr_text.substr(stderr_text.size() - 600);
        result.details = _u8L("the checker did not return a verdict") + " (" + _u8L("exit code") + " "
                       + std::to_string(exit_code) + ")\n" + stderr_text;
        return result;
    }

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

CheckResult cached_check(const CheckerCommand &checker, const ModelObject &object, const ModelInstance &instance)
{
    const std::string key = cache_key(object, instance.get_scaling_factor(), instance.get_mirror());
    {
        std::lock_guard<std::mutex> lock(s_cache_mutex);
        if (auto it = s_cache.find(key); it != s_cache.end())
            return it->second;
    }

    // Model parts merged with their volume transforms, then the instance
    // scaling / mirroring: the shape that would actually be printed.
    TriangleMesh mesh = object.raw_mesh();
    instance.transform_mesh(&mesh, true);
    BOOST_LOG_TRIVIAL(info) << "Firearm gate: checking \"" << object.name << "\" (" << mesh.facets_count() << " facets)";
    const auto  t0     = std::chrono::steady_clock::now();
    CheckResult result = run_checker(checker, mesh, object.name);
    const double secs  = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    BOOST_LOG_TRIVIAL(info) << "Firearm gate: \"" << object.name << "\" -> "
                            << (! result.decided ? "FAILED" : result.blocked ? "BLOCK" : "ALLOW") << " in " << secs << " s";

    if (result.decided) {
        // Failures are not cached, so fixing the checker takes effect at the next plate change.
        std::lock_guard<std::mutex> lock(s_cache_mutex);
        if (s_cache.size() >= s_cache_limit)
            s_cache.clear();
        s_cache[key] = result;
    }
    return result;
}

} // namespace

std::string firearm_gate_validate(const std::vector<const ModelObject*> &objects)
{
    if (objects.empty())
        return {};

    const CheckerCommand checker = checker_command();
    if (checker.empty())
        return _u8L("Slicing is disabled: the firearm-part checker was not found.") + "\n"
             + _u8L("Set PRUSA_FIREARM_CHECK to the checker executable or put \"firearm-check\" on PATH.");

    std::ostringstream blocked, failed;
    for (const ModelObject *object : objects) {
        if (object == nullptr || object->instances.empty())
            continue;
        // One check per distinct instance scaling; most objects have one.
        std::vector<std::string> seen;
        for (const ModelInstance *instance : object->instances) {
            std::ostringstream k;
            k << instance->get_scaling_factor().transpose() << '/' << instance->get_mirror().transpose();
            if (std::find(seen.begin(), seen.end(), k.str()) != seen.end())
                continue;
            seen.emplace_back(k.str());

            const CheckResult r = cached_check(checker, *object, *instance);
            if (! r.decided) {
                failed << "  \"" << object->name << "\": " << r.details << '\n';
                break;
            }
            if (r.blocked) {
                blocked << "  \"" << object->name << "\"\n" << r.details;
                break;
            }
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
    return message;
}

} // namespace Slic3r
