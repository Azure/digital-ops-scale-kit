// upgrade-path.bicep
// -------------------------------------------------------------------------------------
// Import-only library: the AIO upgrade path decision. check-upgrade-path.bicep
// runs it first in aio-upgrade, and update-extensions.bicep runs it again on
// the target it applies, so both refuse the same moves. The functions are
// pure, so tests evaluate them offline with `az bicep snapshot`.
//
// The data is upgrade-paths.yaml: each AIO release in this content with
// Microsoft's "Upgrade From (AIO)" list for it.
//
// Decision, first match wins:
//   installed version or train unreadable  unverified  warning
//   installed equals target                unchanged   pass
//   move between preview and GA            refused
//   target older than installed            refused
//   target not in the table                unverified  warning
//   installed in the target's list         supported   pass
//   otherwise                              refused, naming the next AIO release
//                                          on the shortest listed path, newest
//                                          first hop on a tie
// -------------------------------------------------------------------------------------

@export()
@description('Supported AIO upgrade paths for each AIO release in this content.')
var upgradePaths = loadYamlContent('upgrade-paths.yaml')

@export()
@description('Decides whether aio-upgrade may move an instance from its installed AIO version and train to the target. Returns status, message and nextHop.')
func upgradeDecision(installedVersion string, installedTrain string, targetVersion string, targetTrain string, paths object) object =>
  decide(installedVersion, toLower(installedTrain), targetVersion, toLower(targetTrain), rowsOf(paths))

@export()
@description('Returns the target value, or the installed value when the target is empty, as the extension update applies it.')
func preserved(target string, installed string) string => empty(target) ? installed : target

@export()
@description('Decides on the target that the extension update applies: an empty version or train keeps the installed one.')
func targetDecision(installedVersion string, installedTrain string, aioVersion string, aioTrain string, paths object) object =>
  upgradeDecision(installedVersion, installedTrain, preserved(aioVersion, installedVersion), preserved(aioTrain, installedTrain), paths)

func decide(installed string, installedTrain string, target string, targetTrain string, rows array) object =>
  !isReadableVersion(installed) || empty(installedTrain)
    ? result('unverified', 'aio-upgrade could not read the installed AIO version, so it did not check the upgrade path.', '')
    : installed == target && installedTrain == targetTrain
      ? result('unchanged', '', '')
      : isPreviewTrain(installedTrain) != isPreviewTrain(targetTrain)
        ? result('refused', 'The instance runs AIO ${installed} on the ${installedTrain} train, and ${targetLabel(target, targetTrain, rows)} uses the ${targetTrain} train. Moving between preview and GA needs an uninstall and reinstall.', '')
        : isOlder(target, installed)
          ? result('refused', '${targetLabel(target, targetTrain, rows)} is older than the installed AIO ${installed}. Azure IoT Operations does not support a downgrade. Uninstall and reinstall to use an older version.', '')
          : empty(rowsFor(rows, target, targetTrain))
            ? result('unverified', 'AIO ${target} on the ${targetTrain} train is not an AIO release this content lists, so aio-upgrade did not check the path from AIO ${installed}.', '')
            : decideListed(installed, rowsFor(rows, target, targetTrain)[0], rows)

func decideListed(installed string, row object, rows array) object =>
  contains(row.upgradeFrom, installed)
    ? result('supported', '', '')
    : refusePath(installed, row, nextHop(installed, rows, hopsTo(rows, row.name)))

func refusePath(installed string, row object, hop string) object =>
  result(
    'refused',
    empty(hop)
      ? 'Microsoft\'s supported upgrade list for AIO release ${row.name} (${row.version}) does not include AIO ${installed}, and the AIO releases in this content offer no path from it. Choose a path at https://aka.ms/aio-versions.'
      : 'Microsoft\'s supported upgrade list for AIO release ${row.name} (${row.version}) does not include AIO ${installed}. Upgrade to AIO release ${hop} first.',
    hop
  )

func result(status string, message string, nextHop string) object => {
  status: status
  message: message
  nextHop: nextHop
}

func targetLabel(target string, targetTrain string, rows array) string =>
  empty(rowsFor(rows, target, targetTrain)) ? 'AIO ${target}' : 'AIO release ${rowsFor(rows, target, targetTrain)[0].name} (${target})'

func isPreviewTrain(train string) bool => train == 'preview'

// =====================================================================================
// Table rows and listed paths
// =====================================================================================

func rowsOf(paths object) array => map(items(paths.releases), release => {
  name: release.key
  version: release.value.version
  train: toLower(release.value.train)
  upgradeFrom: release.value.upgradeFrom
})

func rowsFor(rows array, version string, train string) array => filter(rows, row => row.version == version && row.train == train)

// Listed hops from each release to the target, -1 when no listed path exists.
// Each round extends every path by one release, so as many rounds as rows
// cover the longest possible path.
func hopsTo(rows array, target string) object =>
  reduce(range(0, length(rows)), toObject(rows, row => row.name, row => row.name == target ? 0 : -1), (hops, round) => extendHops(rows, hops))

func extendHops(rows array, hops object) object =>
  toObject(rows, row => row.name, row => hops[row.name] >= 0 ? hops[row.name] : fewestHopsFrom(row.version, rows, hops))

func fewestHopsFrom(version string, rows array, hops object) int =>
  empty(reachingFrom(version, rows, hops)) ? -1 : 1 + min(map(reachingFrom(version, rows, hops), row => hops[row.name]))

func reachingFrom(version string, rows array, hops object) array =>
  filter(rows, row => hops[row.name] >= 0 && contains(row.upgradeFrom, version))

// The first release on the shortest listed path from the installed version,
// the newest one on a tie. The target itself is excluded, so it never names
// the release that refused the move.
func nextHop(installed string, rows array, hops object) string =>
  empty(firstHops(installed, rows, hops)) ? '' : newest(fewestHops(firstHops(installed, rows, hops), hops), hops).name

func firstHops(installed string, rows array, hops object) array =>
  filter(rows, row => hops[row.name] > 0 && contains(row.upgradeFrom, installed))

func fewestHops(candidates array, hops object) array =>
  filter(candidates, row => hops[row.name] == min(map(candidates, candidate => hops[candidate.name])))

func newest(candidates array, hops object) object =>
  reduce(candidates, candidates[0], (best, row) => compareVersions(row.version, best.version) > 0 ? row : best)

// =====================================================================================
// Versions: MAJOR.MINOR.PATCH, optionally followed by -LABEL.NUMBER
// =====================================================================================

func isDigits(value string) bool =>
  !empty(value) && empty(reduce(['0', '1', '2', '3', '4', '5', '6', '7', '8', '9'], value, (rest, digit) => replace(rest, digit, '')))

func versionCore(version string) array => split(split(version, '-')[0], '.')

func isReadableVersion(version string) bool =>
  length(versionCore(version)) == 3 && isDigits(versionCore(version)[?0] ?? '') && isDigits(versionCore(version)[?1] ?? '') && isDigits(versionCore(version)[?2] ?? '') && (!contains(version, '-') || isDigits(last(split(version, '.'))))

func isOlder(version string, than string) bool => isReadableVersion(version) ? compareVersions(version, than) < 0 : false

func versionKey(version string) array => [
  int(versionCore(version)[0])
  int(versionCore(version)[1])
  int(versionCore(version)[2])
  contains(version, '-') ? 0 : 1
  contains(version, '-') ? int(last(split(version, '.'))) : 0
]

func compareVersions(left string, right string) int => compareKeys(versionKey(left), versionKey(right))

func compareKeys(left array, right array) int =>
  left[0] != right[0]
    ? order(left[0], right[0])
    : left[1] != right[1]
      ? order(left[1], right[1])
      : left[2] != right[2]
        ? order(left[2], right[2])
        : left[3] != right[3] ? order(left[3], right[3]) : left[4] != right[4] ? order(left[4], right[4]) : 0

func order(left int, right int) int => left < right ? -1 : 1