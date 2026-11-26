# This file is used to manage the dependencies. It is
# used by gclient to determine what version of each dependency to check out, and
# where.
#
# For more information, please refer to the official documentation:
#   https://sites.google.com/a/chromium.org/dev/developers/how-tos/get-the-code
#
# When adding a new dependency, please update the top-level .gitignore file
# to list the dependency's destination directory.
#
# -----------------------------------------------------------------------------
# Rolling deps
# -----------------------------------------------------------------------------
# All repositories in this file are git-based, using Chromium git mirrors where
# necessary (e.g., a git mirror is used when the source project is SVN-based).
# To update the revision that Chromium pulls for a given dependency:
#
#  # Create and switch to a new branch
#  git new-branch depsroll
#  # Run roll-dep (provided by depot_tools) giving the dep's path and optionally
#  # a regex that will match the line in this file that contains the current
#  # revision. The script ALWAYS rolls the dependency to the latest revision
#  # in origin/master. The path for the dep should start with src/.
#  roll-dep src/third_party/foo_package/src foo_package.git
#  # You should now have a modified DEPS file; commit and upload as normal
#  git commit -aspv_he
#  git cl upload
#
# For more on the syntax and semantics of this file, see:
#   https://bit.ly/chromium-gclient-conditionals
#
# which is a bit incomplete but the best documentation we have at the
# moment.

# We expect all git dependencies specified in this file to be in sync with git
# submodules (gitlinks).
git_dependencies = 'SYNC'

use_relative_paths = True

vars = {
  'buildtools_git': 'https://chromium.googlesource.com/chromium/src/buildtools.git',
  'chromium_tsproxy_git': 'https://chromium.googlesource.com/external/github.com/catchpoint/WebPageTest.tsproxy.git',
  'chromium_webpagereplay_git': 'https://chromium.googlesource.com/webpagereplay',
  'clang_format_git': 'https://chromium.googlesource.com/external/github.com/llvm/llvm-project/clang/tools/clang-format.git',
  'hjson_js_git': 'https://chromium.googlesource.com/external/github.com/hjson/hjson-js',
  'speedometer_git': 'https://chromium.googlesource.com/external/github.com/WebKit/Speedometer.git',

  # This variable is overridden in Chromium's DEPS file.
  'build_with_chromium': False,

  'chrome_internal_url': 'https://chrome-internal.googlesource.com',
  'checkout_crossbench_internal': False,

  # Check out press benchmark sources (e.g. third_party/speedometer) so they
  # can be served from a local file server instead of a live URL.
  # Chromium checkouts already provide these under src/third_party.
  'checkout_press_benchmarks': True,

  # Three lines of non-changing comments so that
  # the commit queue can handle CLs rolling crossbench-internal
  # and whatever else without interference from each other.
  'crossbench_internal_revision': '2d67edcde5e1df7cf8cb49e26c8a432ee9f0d3ee',

  # Three lines of non-changing comments so that
  # the commit queue can handle CLs rolling tsproxy
  # and whatever else without interference from each other.
  'buildtools_revision': 'c65dc4e8c262156a4a6f8731952711386d7067f3',
  # Three lines of non-changing comments so that
  # the commit queue can handle CLs rolling tsproxy
  # and whatever else without interference from each other.
  'clang_format_revision': '65b9a76faeba5f10c1f331ad729b3bae89defbbb',
  # Three lines of non-changing comments so that
  # the commit queue can handle CLs rolling tsproxy
  # and whatever else without interference from each other.
  'tsproxy_revision': '7915adec656341bfab173484e1e0ca661eea1627',
  # Three lines of non-changing comments so that
  # the commit queue can handle CLs rolling webpagereplay
  # and whatever else without interference from each other.
  'webpagereplay_revision': '9104210ce3699fa822877d6f29bd4ca4ec8e7b5c',
  # Three lines of non-changing comments so that
  # the commit queue can handle CLs rolling webpagereplay
  # and whatever else without interference from each other.
  'hjson_js_revision': '74bfd14e07d6586e3c12e0d2ab9885a97b2d579c',
  # Three lines of non-changing comments so that
  # the commit queue can handle CLs rolling Speedometer v3.1
  # and whatever else without interference from each other.
  'speedometer_3.1_revision': '1386415be8fef2f6b6bbdbe1828872471c5d802a',

  'nodejs_version': '26.13.1',
}

# Only these hosts are allowed for dependencies in this DEPS file.
# If you need to add a new host, contact chrome infrastructure team.
allowed_hosts = [
  'chrome-internal.googlesource.com',
  'chromium.googlesource.com',
]

deps = {
  'buildtools': {
    'url': Var('buildtools_git') + '@' + Var('buildtools_revision'),
    'condition': 'not build_with_chromium',
  },
  'third_party/clang-format/script': {
    'url': Var('clang_format_git') + '@' + Var('clang_format_revision'),
    'condition': 'not build_with_chromium',
  },
  'third_party/hjson_js': Var('hjson_js_git') + '@' + Var('hjson_js_revision'),
  'third_party/node': {
    'packages': [
      {
        'package': 'infra/3pp/tools/nodejs/${{platform}}',
        # `3` is the 3pp CIPD package epoch (not part of the Node.js version).
        'version': 'version:3@{nodejs_version}',
      },
    ],
    'dep_type': 'cipd',
    'condition': 'not build_with_chromium',
  },
  'third_party/tsproxy': Var('chromium_tsproxy_git') + '@' + Var('tsproxy_revision'),
  'third_party/speedometer/v3.1': {
    'url': Var('speedometer_git') + '@' + Var('speedometer_3.1_revision'),
    'condition': 'checkout_press_benchmarks and not build_with_chromium',
  },
  'third_party/webpagereplay': {
    'url': Var('chromium_webpagereplay_git') + '@' + Var('webpagereplay_revision'),
    'condition': 'not build_with_chromium',
  },
  'internal': {
    'url': Var('chrome_internal_url') + '/crossbench/crossbench-internal.git' + '@' + Var('crossbench_internal_revision'),
    'condition': 'checkout_crossbench_internal',
  }
}

hooks = [
  {
    'name': 'vpython3_common',
    'pattern': '.',
    'action': [ 'vpython3',
                '-vpython-tool', 'install',
    ],
  },
]

recursedeps = [
  # Required to get the clang-format binaries via buildtools/DEPS.
  'buildtools',
  # WebPageReplay includes a go compiler in its DEPS.
  'third_party/webpagereplay',
]
