import { test } from 'node:test';
import { validate } from './match-pairs.mjs';

test('calibration corpus has resolvable checksummed photos, documented labels and balanced modalities', async () => {
  await validate();
});
