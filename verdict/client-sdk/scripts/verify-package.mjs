import { existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const packageRoot = fileURLToPath(new URL('../', import.meta.url));
const requiredFiles = ['dist/index.js', 'dist/index.d.ts'];
const missingFiles = requiredFiles.filter(file => !existsSync(join(packageRoot, file)));

if (missingFiles.length > 0) {
  throw new Error(`Package build is missing required files: ${missingFiles.join(', ')}`);
}

const output = execFileSync('npm', ['pack', '--dry-run', '--json'], {
  cwd: packageRoot,
  encoding: 'utf8',
});
const [packageInfo] = JSON.parse(output);
const packedFiles = new Set(packageInfo.files.map(file => file.path));
const missingPackedFiles = requiredFiles.filter(file => !packedFiles.has(file));

if (missingPackedFiles.length > 0) {
  throw new Error(`Package tarball is missing required files: ${missingPackedFiles.join(', ')}`);
}
