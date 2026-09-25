#!/usr/bin/env node
/**
 * Consumer smoke test for @bodanglin/verdict-contracts
 *
 * Fully isolated: creates a temp dir OUTSIDE the repo, packs the tarball there,
 * installs it with --no-save --no-package-lock to avoid polluting the repo.
 * 
 * VALIDATES: the packaged package.json has no self-dependencies, file:/link:/workspace:
 * specifiers, or unexpected runtime dependencies.
 */

import { readFile, rm, mkdir, writeFile } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import crypto from 'node:crypto';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const contractsRoot = join(__dirname, '..');

let tempDir = null;

function sha256(data) {
  return crypto.createHash('sha256').update(data).digest('hex');
}

async function cleanup() {
  if (tempDir && existsSync(tempDir)) {
    try {
      await rm(tempDir, { recursive: true, force: true });
      console.log(`✓ Cleaned up ${tempDir}`);
    } catch (err) {
      console.warn(`Warning: Could not clean up ${tempDir}: ${err.message}`);
    }
  }
}

process.on('exit', () => {
  if (tempDir && existsSync(tempDir)) {
    console.log('Note: Temp directory not cleaned up, please remove manually:', tempDir);
  }
});

async function main() {
  try {
    console.log('=== Consumer Smoke Test ===\n');

    // Step 1: Build the package
    console.log('1. Building package...');
    execSync('npm run build', { cwd: contractsRoot, stdio: 'inherit' });
    console.log('✓ Build successful\n');

    // Step 2: Create isolated temp directory OUTSIDE the repo
    console.log('2. Creating isolated temp directory...');
    tempDir = await mkdtemp(join(tmpdir(), 'verdict-contracts-smoke-'));
    console.log(`✓ Created temp dir: ${tempDir}\n`);

    // Step 3: Pack tarball into temp dir
    console.log('3. Creating tarball...');
    const packOutput = execSync(`npm pack --pack-destination ${tempDir}`, { 
      cwd: contractsRoot, 
      encoding: 'utf8' 
    });
    const tarballName = packOutput.trim().split('\n').pop();
    const tarballPath = join(tempDir, tarballName);
    console.log(`✓ Created ${tarballName}\n`);

    // Step 4: Validate packaged package.json
    console.log('4. Validating packaged package.json...');
    const packageJsonInTarball = execSync(`tar -xzOf ${tarballName} package/package.json`, {
      cwd: tempDir,
      encoding: 'utf8'
    });
    const packagedPkg = JSON.parse(packageJsonInTarball);
    
    // Check for self-dependency
    if (packagedPkg.dependencies && packagedPkg.dependencies[packagedPkg.name]) {
      console.error(`  ✗ Package depends on itself: ${packagedPkg.name}`);
      process.exit(1);
    }
    
    // Check for file:, link:, workspace: specifiers
    const allDeps = {
      ...packagedPkg.dependencies,
      ...packagedPkg.devDependencies,
      ...packagedPkg.peerDependencies,
      ...packagedPkg.optionalDependencies
    };
    for (const [dep, spec] of Object.entries(allDeps)) {
      if (typeof spec === 'string' && (spec.startsWith('file:') || spec.startsWith('link:') || spec.startsWith('workspace:'))) {
        console.error(`  ✗ Forbidden dependency specifier: ${dep}: ${spec}`);
        process.exit(1);
      }
    }
    
    // Check that runtime dependencies are allowed (none expected for this package)
    const allowedRuntimeDeps = new Set([]); // No runtime deps expected
    if (packagedPkg.dependencies) {
      for (const dep of Object.keys(packagedPkg.dependencies)) {
        if (!allowedRuntimeDeps.has(dep)) {
          console.error(`  ✗ Unexpected runtime dependency: ${dep}`);
          console.error(`    Allowed: ${Array.from(allowedRuntimeDeps).join(', ') || '(none)'}`);
          process.exit(1);
        }
      }
    }
    
    console.log('  ✓ Packaged package.json valid (no self-deps, file: specifiers, or unexpected deps)');

    // Step 5: Create minimal package.json in temp dir
    console.log('\n5. Setting up test environment...');
    const minimalPkg = {
      name: 'smoke',
      private: true,
      type: 'module'
    };
    await writeFile(join(tempDir, 'package.json'), JSON.stringify(minimalPkg, null, 2));
    
    // Install zod and the tarball with --no-save --no-package-lock --ignore-scripts
    execSync('npm install zod@^3.23.0 --no-save --no-package-lock --ignore-scripts', { 
      cwd: tempDir, 
      stdio: 'inherit' 
    });
    execSync(`npm install ${tarballPath} --no-save --no-package-lock --ignore-scripts`, { 
      cwd: tempDir, 
      stdio: 'inherit' 
    });
    console.log('✓ Package installed\n');

    // Step 6: Create test script
    const testScript = `import { parseContract } from '@bodanglin/verdict-contracts';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';

function sha256(data) {
  return createHash('sha256').update(data).digest('hex');
}

const v1ManifestPath = './node_modules/@bodanglin/verdict-contracts/fixtures/execution-envelope/v1/manifest.json';
const v1Manifest = JSON.parse(readFileSync(v1ManifestPath, 'utf8'));

console.log('  Testing ExecutionEnvelope v1 fixtures:');
for (const [filename, fixtureSpec] of Object.entries(v1Manifest.fixtures)) {
  const fixturePath = \`./node_modules/@bodanglin/verdict-contracts/fixtures/execution-envelope/v1/\${filename}\`;
  const fixtureData = readFileSync(fixturePath, 'utf8');
  const fixtureJson = JSON.parse(fixtureData);

  const actualSha = sha256(fixtureData);
  if (actualSha !== fixtureSpec.sha256) {
    console.error(\`    ✗ \${filename}: sha256 mismatch\`);
    console.error(\`      Expected: \${fixtureSpec.sha256}\`);
    console.error(\`      Actual:   \${actualSha}\`);
    process.exit(1);
  }

  const shouldRejectSchema = filename === 'unknown-field.json';
  try {
    const parsed = parseContract('ExecutionEnvelope', fixtureJson);
    if (shouldRejectSchema) {
      console.error(\`    ✗ \${filename}: expected schema rejection but parse succeeded\`);
      process.exit(1);
    }
    console.log(\`    ✓ \${filename}: schema valid (sha256 verified)\`);
  } catch (err) {
    if (shouldRejectSchema) {
      console.log(\`    ✓ \${filename}: schema rejected as expected\`);
    } else {
      console.error(\`    ✗ \${filename}: unexpected schema parse failure\`);
      console.error(\`      Error: \${err.message}\`);
      process.exit(1);
    }
  }
}

console.log('');
console.log('  Testing fixture exports path:');
const manifestViaExport = await import('@bodanglin/verdict-contracts/fixtures/execution-envelope/v1/manifest.json', { with: { type: 'json' } });
const manifestData = manifestViaExport.default;
if (manifestData.contract_version !== '1') {
  console.error('    ✗ Fixture export path failed: invalid manifest');
  process.exit(1);
}
console.log('    ✓ Fixture manifest loaded via @bodanglin/verdict-contracts/fixtures/...');

console.log('');
console.log('=== Fixture Tests Passed ✓ ===');
`;

    await writeFile(join(tempDir, 'test.mjs'), testScript);

    console.log('6. Testing package imports and fixture parsing...');
    execSync('node test.mjs', { cwd: tempDir, stdio: 'inherit' });

    // Step 7: Check tarball file list
    console.log('\n7. Verifying tarball contents...');
    const tarballFiles = execSync(`tar -tzf ${tarballName}`, {
      cwd: tempDir,
      encoding: 'utf8',
    })
      .trim()
      .split('\n')
      .map(f => f.replace(/^package\//, ''));

    const allowedPatterns = [
      'package.json',
      'README.md',
      'CHANGELOG.md',
      /^dist\//,
      /^fixtures\//,
    ];

    const forbidden = ['tests/', 'src/', 'test/', '__tests__/'];

    const disallowed = [];
    for (const file of tarballFiles) {
      if (forbidden.some(f => file.startsWith(f))) {
        disallowed.push(file);
        continue;
      }
      const allowed = allowedPatterns.some(p => {
        if (typeof p === 'string') return file === p;
        return p.test(file);
      });
      if (!allowed) {
        console.warn(`  Warning: unexpected file in tarball: ${file}`);
      }
    }

    if (disallowed.length > 0) {
      console.error('  ✗ Tarball contains forbidden files:');
      disallowed.forEach(f => console.error(`    - ${f}`));
      process.exit(1);
    }

    const requiredFiles = ['package.json', 'README.md', 'CHANGELOG.md'];
    for (const required of requiredFiles) {
      if (!tarballFiles.includes(required)) {
        console.error(`  ✗ Tarball missing required file: ${required}`);
        process.exit(1);
      }
    }

    if (!tarballFiles.some(f => f.startsWith('dist/'))) {
      console.error('  ✗ Tarball missing dist/ directory');
      process.exit(1);
    }
    if (!tarballFiles.some(f => f.startsWith('fixtures/'))) {
      console.error('  ✗ Tarball missing fixtures/ directory');
      process.exit(1);
    }

    console.log('  ✓ Tarball contents valid (no src/ or tests/, dist/ and fixtures/ present)');
    console.log(`  ✓ File count: ${tarballFiles.length}`);

    console.log('\n=== All Smoke Tests Passed ✓ ===');

  } catch (err) {
    console.error('\n✗ Smoke test failed:', err.message);
    if (err.stack) console.error(err.stack);
    process.exit(1);
  } finally {
    await cleanup();
  }
}

main();
