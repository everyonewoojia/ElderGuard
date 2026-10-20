const api = require('../../utils/api');
const { createBoundPage } = require('../../utils/bound-page');

Page(createBoundPage(async (id) => {
  const rows = await api.get('/tasks');
  const tasks = rows.filter((task) => task.elder_id === id);
  return { tasks, empty: tasks.length === 0 };
}));
