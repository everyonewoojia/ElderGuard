const api = require('../../utils/api');
const { createBoundPage } = require('../../utils/bound-page');

Page(createBoundPage(async (id) => {
  const elder = await api.get(`/elder/${id}`);
  if (!elder || elder.id !== id || typeof elder.name !== 'string') {
    throw new Error('Invalid elder response');
  }
  return { elder, empty: false };
}));
